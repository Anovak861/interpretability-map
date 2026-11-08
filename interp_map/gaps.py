"""Targeted ingestion: find documents that test the claims the field relies on but the map
has little evidence for.

1. `evidence_gaps()` ranks claims by direct reliance (documents that assume them + claims
   that depend on them) among those not independently replicated.
2. `suggest_sources()` asks a model, in batches, which published works test, replicate or
   challenge each claim. Suggestions are only leads: every one is verified against arXiv by
   title before it becomes a candidate, so an invented or misremembered paper is dropped.
3. Verified works become accepted candidates that remember their target claims; extraction
   then shows those claims to the model as `focus_claims`, so evidence attaches to them
   instead of creating near-duplicates.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from typing import Literal, Optional

from pydantic import BaseModel, Field

from . import db, extract, fetchers, pipeline, report


class Suggestion(BaseModel):
    claim: str = Field(description="claim id from the list, e.g. g3")
    title: str = Field(description="exact title of the published work")
    arxiv_id: Optional[str] = Field(None, description="arXiv id like 2306.04719 if it is on arXiv, else null")
    url: Optional[str] = Field(None, description="canonical URL if not on arXiv (blog post, Distill, transformer-circuits), else null")
    expected: Literal["supports", "refutes", "mixed", "replicates", "fails_to_replicate"]
    why: str = Field(description="at most 15 words: what it tests")


class SuggestionList(BaseModel):
    suggestions: list[Suggestion]


INSTRUCTIONS = """\
You are helping fill evidence gaps in a research map of LLM interpretability. The claims
below are relied on by many papers but the map has little or no independent evidence for
them. For each claim, list published works (papers, or substantial research posts) that
EMPIRICALLY test, replicate, or challenge it, including works that find problems with it.
Interpretability illusions, sanity checks, benchmarks and replication studies are especially
valuable.

Rules:
- List only works you are confident exist, with their exact titles. Give the arXiv id when
  the work is on arXiv. Do not guess ids: if unsure of the id, give the title and null id.
- Up to 4 works per claim, best first; an empty list for a claim is fine. A work may be
  listed for several claims.
- "expected" is your best guess of how the work bears on the claim.
- Refer to claims only by their id. Output JSON matching the schema, and nothing else.
"""


def evidence_gaps(conn, limit: int = 60, min_reliance: int = 3, offset: int = 0) -> list[dict]:
    health = report.claim_health(conn)
    direct = Counter(r[0] for r in conn.execute("SELECT depends_on_id FROM claim_dependencies"))
    rows = [{"id": h.id, "slug": h.slug, "statement": h.statement, "status": h.status,
             "reliance": h.assumed_by + direct[h.id], "positive": h.positive_docs, "negative": h.negative_docs}
            for h in health.values()
            if h.status in ("untested", "single_source", "supported", "contested")
            and h.assumed_by + direct[h.id] >= min_reliance]
    return sorted(rows, key=lambda r: -r["reliance"])[offset:offset + limit]


def _ask(claims: list[dict], backend: str) -> list[tuple[dict, Suggestion]]:
    ids = {f"g{i}": c for i, c in enumerate(claims, 1)}
    prompt = (f"{INSTRUCTIONS}\n<schema>\n{json.dumps(SuggestionList.model_json_schema())}\n</schema>\n\n<claims>\n" +
              "\n".join(f"{k} | {c['status']} | {c['statement']}" for k, c in ids.items()) + "\n</claims>\n")
    tel = {"purpose": "gap-sources"}
    if backend == "codex_cli":
        model, effort = extract.codex_settings()
        raw = extract.run_codex_cli(prompt, SuggestionList.model_json_schema(), model=model, reasoning_effort=effort, telemetry=tel)
    else:
        raw = extract.run_claude_cli(prompt, telemetry=tel)
    m = re.search(r"\{.*\}", raw, re.S)
    result = SuggestionList.model_validate_json(m.group(0) if m else raw)
    return [(ids[s.claim.strip().lower()], s) for s in result.suggestions if s.claim.strip().lower() in ids]


def suggest_sources(conn, gaps: list[dict], *, backend: str = "claude_cli", batch: int = 15, log=print) -> list[dict]:
    """Leads from the model, verified against arXiv. Returns verified works with target claims."""
    leads: dict[str, dict] = {}
    for i in range(0, len(gaps), batch):
        for claim, s in _ask(gaps[i:i + batch], backend):
            key = (s.arxiv_id or "").strip() or db._norm_title(s.title)
            lead = leads.setdefault(key, {"title": s.title.strip(), "arxiv_id": (s.arxiv_id or "").strip() or None,
                                          "url": s.url, "targets": {}})
            lead["targets"][claim["slug"]] = {"expected": s.expected, "why": s.why}
        log(f"  asked about claims {i + 1}-{min(i + batch, len(gaps))}: {len(leads)} distinct works suggested so far")

    # verification: an arXiv id must resolve to a paper with the suggested title
    with_id = [l for l in leads.values() if l["arxiv_id"] and re.fullmatch(r"\d{4}\.\d{4,5}", l["arxiv_id"])]
    meta = {}
    for aid in [l["arxiv_id"] for l in with_id]:
        try:
            meta[aid] = fetchers._arxiv_meta_from_abs_page(aid)
        except Exception:
            pass
    verified, rejected = [], []
    for l in leads.values():
        m = meta.get(l["arxiv_id"]) if l["arxiv_id"] else None
        if m and pipeline.title_matches([l["title"]], m.title):
            verified.append({**l, "title": m.title, "url": f"https://arxiv.org/abs/{l['arxiv_id']}"})
        elif l.get("url") and not l["arxiv_id"] and re.match(r"https://(transformer-circuits\.pub|distill\.pub|www\.lesswrong\.com|www\.alignmentforum\.org)/", l["url"]):
            verified.append({**l, "verify_title_on_fetch": True})   # checked against the page title when fetched
        else:
            rejected.append(l)
    log(f"  verified {len(verified)} works; dropped {len(rejected)} that didn't match arXiv (wrong id, or no id given)")
    return verified


def queue_verified(conn, works: list[dict]) -> dict:
    """Add verified works as accepted candidates that remember their target claims."""
    db._add_column(conn, "candidates", "target_claims", "TEXT")
    stats = Counter()
    for w in works:
        existing = db.find_document(conn, arxiv_id=w.get("arxiv_id"), url=w["url"], title=w["title"])
        if existing is not None and existing["status"] != "stub":
            stats["already in the map"] += 1
            continue
        # non-arXiv pages are title-checked when fetched (ingest_candidates checks "canon: " reasons)
        reason = f"canon: {w['title']}" if w.get("verify_title_on_fetch") else "evidence gap"
        pipeline.add_candidate(conn, url=w["url"], title=w["title"], source="gap", reason=reason)
        url = db.normalize_url(w["url"])
        row = conn.execute("SELECT id, status, target_claims FROM candidates WHERE url = ?", (url,)).fetchone()
        if row is None:
            continue
        targets = {**json.loads(row["target_claims"] or "{}"), **w["targets"]}
        conn.execute("UPDATE candidates SET target_claims = ?, relevance = 3, "
                     "status = CASE WHEN status IN ('new', 'rejected') THEN 'accepted' ELSE status END WHERE id = ?",
                     (json.dumps(targets), row["id"]))
        stats["queued"] += 1
    conn.commit()
    return dict(stats)


def focus_claims_for(conn, doc_id: int) -> list[dict]:
    """Target claims remembered for a document that came in through gap-filling."""
    if "target_claims" not in {r[1] for r in conn.execute("PRAGMA table_info(candidates)")}:
        return []
    row = conn.execute("SELECT target_claims FROM candidates WHERE document_id = ? AND target_claims IS NOT NULL",
                       (doc_id,)).fetchone()
    if not row:
        return []
    out = []
    for slug, info in json.loads(row[0]).items():
        live = db.resolve_slug(conn, "claim", slug)
        r = conn.execute("SELECT slug, statement FROM claims WHERE slug = ?", (live,)).fetchone() if live else None
        if r:
            out.append({"slug": r["slug"], "statement": r["statement"], "may_bear_on_it_as": info.get("expected")})
    return out


# ---------------------------------------------------------------------------------------------
# Re-check: suggested works that were already in the map but never linked to the claims they
# were suggested for (usually because they were extracted before those claims existed).

LEADS_PATH = db.ROOT / "data" / "gap_recheck_leads.json"


def mapped_leads(conn, claim_slugs: list[str], *, backend: str = "claude_cli", log=print) -> dict:
    """{doc_slug: {claim_slug: {expected, why}}} for suggested works already extracted."""
    health = {h.slug: h for h in report.claim_health(conn).values()}
    claims = []
    for s in claim_slugs:
        h = health.get(db.resolve_slug(conn, "claim", s) or "")
        if h:
            claims.append({"slug": h.slug, "statement": h.statement, "status": h.status})
    out: dict = {}
    for w in suggest_sources(conn, claims, backend=backend, log=log):
        d = db.find_document(conn, arxiv_id=w.get("arxiv_id"), url=w["url"], title=w["title"])
        if d is not None and d["status"] in ("extracted", "reviewed"):
            out.setdefault(d["slug"], {}).update(w["targets"])
    # merge with earlier leads; a document gets re-checked again only for claims it hasn't been asked about
    state = json.loads(LEADS_PATH.read_text(encoding="utf-8")) if LEADS_PATH.exists() else {"leads": {}, "done": []}
    done = set(state["done"])
    for doc_slug, targets in out.items():
        old = state["leads"].setdefault(doc_slug, {})
        if set(targets) - set(old):
            done.discard(doc_slug)
        old.update(targets)
    LEADS_PATH.write_text(json.dumps({"leads": state["leads"], "done": sorted(done)}, indent=1), encoding="utf-8")
    return out


class Finding(BaseModel):
    doc: str = Field(description="document id, e.g. d2")
    claim: str = Field(description="claim id, e.g. c3")
    relation: Literal["supports", "refutes", "mixed", "replicates", "fails_to_replicate", "extends", "critiques", "none"]
    strength: Optional[Literal["weak", "moderate", "strong"]] = None
    experiment_summary: Optional[str] = Field(None, description="what the document did that bears on the claim, 1-2 sentences")
    location: Optional[str] = Field(None, description="section / figure, if visible")


class FindingList(BaseModel):
    findings: list[Finding]


RECHECK_INSTRUCTIONS = """\
You are checking whether research documents provide evidence on specific claims in a map of
LLM interpretability research. For each document you get its summary and the passages of its
text most related to the claims. For every (document, claim) pair listed under a document,
decide how the document's own results bear on the claim:
- supports / refutes / mixed: its experiments test the claim and come out that way.
- replicates / fails_to_replicate: it reruns an earlier test of the claim.
- extends: its results generalize the claim to a new setting.
- critiques: it argues against the claim (conceptually or with evidence) without a direct test.
- none: it does not genuinely bear on the claim. This is the right answer whenever the link
  would be a stretch; never force one.
Strength reflects how directly and convincingly the results bear on the claim. Describe the
relevant experiment concretely. Refer to documents and claims only by their ids. Output JSON
matching the schema, and nothing else.
"""

_WORD = re.compile(r"[a-z][a-z0-9\-]{3,}")


def _passages(text: str, claims: list[str], budget: int = 18000) -> str:
    """The paragraphs sharing the most vocabulary with the claims, in document order."""
    terms = Counter(w for c in claims for w in set(_WORD.findall(c.lower())))
    paras = [p.strip()[:4000] for p in re.split(r"\n\s*\n", text or "") if len(p.strip()) > 80]
    ranked = sorted(range(len(paras)), key=lambda i: -sum(terms[w] for w in set(_WORD.findall(paras[i].lower()))))
    keep, used = set(), 0
    for i in ranked:
        if used + len(paras[i]) <= budget:
            keep.add(i)
            used += len(paras[i])
    return "\n\n".join(paras[i] for i in sorted(keep))


def _recheck_prompt(conn, batch: list[tuple[str, dict]]) -> tuple[str, dict, dict]:
    docs, claims, claim_ids, parts = {}, {}, {}, []
    for j, (doc_slug, targets) in enumerate(batch, 1):
        d = conn.execute("SELECT id, title, abstract, summary, full_text FROM documents WHERE slug = ?", (doc_slug,)).fetchone()
        did = f"d{j}"
        docs[did] = d["id"]
        lines = []
        for slug in targets:
            r = conn.execute("SELECT id, statement FROM claims WHERE slug = ?", (slug,)).fetchone()
            if r is None:
                continue
            if r["id"] not in claim_ids:
                claim_ids[r["id"]] = f"c{len(claim_ids) + 1}"
                claims[claim_ids[r["id"]]] = r["id"]
            lines.append(f"{claim_ids[r['id']]} | {r['statement']}")
        parts.append(f"<document id=\"{did}\">\nTitle: {d['title']}\n\nAbstract: {d['abstract'] or ''}\n\n"
                     f"Summary:\n{d['summary'] or ''}\n\nRelevant passages:\n{_passages(d['full_text'], lines)}\n\n"
                     "Claims to check for this document:\n" + "\n".join(lines) + "\n</document>")
    prompt = (f"{RECHECK_INSTRUCTIONS}\n<schema>\n{json.dumps(FindingList.model_json_schema())}\n</schema>\n\n" +
              "\n\n".join(parts))
    return prompt, docs, claims


def recheck(conn, *, backend: str = "claude_cli", batch: int = 3, workers: int = 3, log=print) -> dict:
    """Run the re-check over the saved leads (resumable: finished documents are recorded)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    state = json.loads(LEADS_PATH.read_text(encoding="utf-8"))
    done = set(state["done"])
    todo = []
    for doc_slug, targets in state["leads"].items():
        d = conn.execute("SELECT id, curated FROM documents WHERE slug = ?", (doc_slug,)).fetchone()
        if d is None or doc_slug in done or d["curated"]:
            continue
        live = {}
        for slug in targets:
            s = db.resolve_slug(conn, "claim", slug)
            r = conn.execute("SELECT id FROM claims WHERE slug = ?", (s,)).fetchone() if s else None
            # a pair with a tested relation already recorded needs no re-check
            if r and not conn.execute("SELECT 1 FROM claim_evidence WHERE claim_id = ? AND document_id = ? "
                                      "AND relation NOT IN ('assumes', 'proposes')", (r["id"], d["id"])).fetchone():
                live[s] = targets[slug]
        if live:
            todo.append((doc_slug, live))
        else:
            done.add(doc_slug)
    batches = [todo[i:i + batch] for i in range(0, len(todo), batch)]
    log(f"  {len(todo)} documents to re-check in {len(batches)} calls")
    prompts = [_recheck_prompt(conn, b) for b in batches]
    provenance = "gap-recheck:codex" if backend == "codex_cli" else f"gap-recheck:{extract.MODEL}"
    stats = Counter()

    def run(prompt):
        tel = {"purpose": "gap-recheck"}
        if backend == "codex_cli":
            model, effort = extract.codex_settings()
            return extract.run_codex_cli(prompt, FindingList.model_json_schema(), model=model,
                                         reasoning_effort=effort, telemetry=tel)
        return extract.run_claude_cli(prompt, telemetry=tel)

    with ThreadPoolExecutor(workers) as pool:
        futs = {pool.submit(run, p[0]): (b, p) for b, p in zip(batches, prompts)}
        for fut in as_completed(futs):
            b, (_, docs, claims) = futs[fut]
            try:
                raw = fut.result()
            except (extract.UsageLimitError, extract.NotLoggedInError) as e:
                if not stats["stopped"]:
                    log(f"  STOP: {e}")
                stats["stopped"] = 1
                for f in futs:
                    f.cancel()
                continue
            except Exception as e:
                stats["failed calls"] += 1
                log(f"  failed: {type(e).__name__}: {e}")
                continue
            m = re.search(r"\{.*\}", raw, re.S)
            try:
                found = FindingList.model_validate_json(m.group(0) if m else raw).findings
            except Exception as e:
                stats["unparsed calls"] += 1
                log(f"  unparsed: {e}")
                continue
            for f in found:
                doc_id, claim_id = docs.get(f.doc.strip().lower()), claims.get(f.claim.strip().lower())
                if doc_id is None or claim_id is None:
                    continue
                if f.relation == "none":
                    stats["no bearing"] += 1
                    continue
                conn.execute("""INSERT OR IGNORE INTO claim_evidence
                                (claim_id, document_id, relation, strength, experiment_summary, location, provenance)
                                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                             (claim_id, doc_id, f.relation, f.strength, f.experiment_summary, f.location, provenance))
                stats[f.relation] += 1
            done.update(slug for slug, _ in b)
            conn.commit()
            LEADS_PATH.write_text(json.dumps({"leads": state["leads"], "done": sorted(done)}, indent=1), encoding="utf-8")
            log(f"  re-checked {len(done)} documents: {dict(stats)}")
    return dict(stats)
