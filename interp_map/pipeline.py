"""Automated upkeep: discover -> triage -> fetch -> extract (parallel waves) -> report.

`update()` is what the scheduled task runs. Every stage is also callable on its own
from the CLI, and every stage is safe to re-run: discovery dedupes on URL, triage
only scores untriaged candidates, extraction only picks up `fetched` documents.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from bs4 import BeautifulSoup

from . import db, extract, fetchers, report
from .db import ROOT
from .fetchers import Fetched, _client
from .models import DocumentMeta, ReferenceRef

CONFIG_PATH = ROOT / "config.json"
LOG_DIR = ROOT / "data" / "logs"
LOCK_PATH = ROOT / "data" / "update.lock"
MAX_EXTRACTION_FAILURES = 3

log: Callable[[str], None] = lambda msg: print(msg, flush=True)


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _get_state(conn, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _set_state(conn, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (key, value))
    conn.commit()


def _since(conn, key: str, cfg: dict) -> datetime:
    """Start of the discovery window for a source (last run, or the initial lookback)."""
    last = _get_state(conn, key)
    if last:
        return datetime.fromisoformat(last) - timedelta(days=1)  # overlap; dedupe handles repeats
    return datetime.now(timezone.utc) - timedelta(days=cfg["discovery"]["initial_lookback_days"])


# ---------------------------------------------------------------- fetched records

def store_fetched(conn, f: Fetched) -> int:
    m = f.meta
    doc_id = db.upsert_document(
        conn, title=m.title, doc_type=m.doc_type, source=m.source, url=m.url, arxiv_id=m.arxiv_id,
        published_date=m.published_date, abstract=m.abstract, full_text=f.text,
        external_ids=json.dumps(f.external_ids) if f.external_ids else None, status="fetched",
    )
    db.set_authors(conn, doc_id, m.authors)
    for ref in f.references:
        db.add_reference(conn, doc_id, ref, f"fetch:{m.source}")
    conn.commit()
    return doc_id


def fetched_from_db(conn, row) -> Fetched:
    """Rebuild a Fetched record from a stored document, including its known citations."""
    authors = [r[0] for r in conn.execute(
        "SELECT a.name FROM document_authors da JOIN authors a ON a.id = da.author_id "
        "WHERE da.document_id = ? ORDER BY da.position", (row["id"],))]
    meta = DocumentMeta(title=row["title"], doc_type=row["doc_type"], source=row["source"], url=row["url"],
                        arxiv_id=row["arxiv_id"], published_date=row["published_date"],
                        abstract=row["abstract"], authors=authors)
    refs = [ReferenceRef(title=r["title"], url=r["url"], arxiv_id=r["arxiv_id"]) for r in conn.execute(
        "SELECT d.title, d.url, d.arxiv_id FROM document_links l JOIN documents d ON d.id = l.cited_id "
        "WHERE l.citing_id = ? AND l.link_type = 'explicit_citation'", (row["id"],))]
    return Fetched(meta, row["full_text"] or "", refs)


# ---------------------------------------------------------------- discovery

def add_candidate(conn, *, url: str, title: str, source: str, published_date=None, authors=None,
                  abstract=None, signal=None, reason=None) -> bool:
    """Record a candidate unless we already have it (as a candidate or an ingested document)."""
    url = db.normalize_url(url)
    existing = db.find_document(conn, url=url, arxiv_id=fetchers.arxiv_id_or_none(url))
    if existing is not None and existing["status"] != "stub":
        return False
    row = conn.execute("SELECT id, status FROM candidates WHERE url = ?", (url,)).fetchone()
    if row:
        # refresh signals that grow over time (karma, citation counts); keep the decision
        conn.execute("UPDATE candidates SET signal = COALESCE(?, signal) WHERE id = ?", (signal, row["id"]))
        return False
    conn.execute(
        """INSERT INTO candidates (url, title, source, published_date, authors, abstract, signal, reason)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (url, title, source, published_date, json.dumps(authors or []), abstract, signal, reason))
    return True


ATOM = {"a": "http://www.w3.org/2005/Atom"}


def discover_arxiv(conn, cfg: dict) -> int:
    c = cfg["discovery"]["arxiv"]
    since = _since(conn, "discover.arxiv", cfg)
    phrases = " OR ".join(f'abs:"{p}"' for p in c["phrases"])
    cats = " OR ".join(f"cat:{x}" for x in c["categories"])
    new, start, page = 0, 0, 100
    while start < c["max_results"]:
        r = fetchers.polite_get("https://export.arxiv.org/api/query", params={
            "search_query": f"({phrases}) AND ({cats})", "sortBy": "submittedDate",
            "sortOrder": "descending", "start": start, "max_results": page})
        r.raise_for_status()
        entries = ET.fromstring(r.text).findall("a:entry", ATOM)
        reached_end = not entries
        for e in entries:
            published = e.findtext("a:published", "", ATOM)
            if published and datetime.fromisoformat(published.replace("Z", "+00:00")) < since:
                reached_end = True
                break
            aid = fetchers.arxiv_id_from(e.findtext("a:id", "", ATOM))
            new += add_candidate(
                conn, url=f"https://arxiv.org/abs/{aid}", source="arxiv",
                title=" ".join(e.findtext("a:title", "", ATOM).split()),
                published_date=published[:10] or None,
                authors=[a.findtext("a:name", "", ATOM) for a in e.findall("a:author", ATOM)],
                abstract=" ".join(e.findtext("a:summary", "", ATOM).split()), reason="arxiv keyword search")
        conn.commit()
        if reached_end:
            break
        start += page
    _set_state(conn, "discover.arxiv", datetime.now(timezone.utc).isoformat())
    return new


_LW_LIST = """query ($after: String, $before: String) { posts(input: {terms: {view: "new", limit: 300,
  after: $after, before: $before}}) { results { _id title url postedAt baseScore af
  user { displayName } coauthors { displayName } tags { _id } contents { plaintextDescription } } } }"""


def discover_lesswrong(conn, cfg: dict) -> int:
    c = cfg["discovery"]["lesswrong"]
    since = _since(conn, "discover.lesswrong", cfg) - timedelta(days=14)  # re-check karma of recent posts
    settled = datetime.now(timezone.utc) - timedelta(days=c["settle_days"])
    tag_ids, new = set(c["tag_ids"]), 0
    before = None
    while True:  # page backwards in time; the API caps each page
        r = _client.post("https://www.lesswrong.com/graphql", json={
            "query": _LW_LIST, "variables": {"after": since.date().isoformat(), "before": before}})
        r.raise_for_status()
        posts = r.json()["data"]["posts"]["results"]
        for p in posts:
            if not tag_ids & {t["_id"] for t in p.get("tags") or []}:
                continue
            posted = datetime.fromisoformat(p["postedAt"].replace("Z", "+00:00"))
            threshold = c["min_karma_alignment_forum"] if p.get("af") else c["min_karma"]
            if posted > settled or (p.get("baseScore") or 0) < threshold:
                continue  # too new to judge, or below the bar (may qualify on a later run)
            authors = [p["user"]["displayName"]] + [a["displayName"] for a in p.get("coauthors") or []]
            snippet = ((p.get("contents") or {}).get("plaintextDescription") or "")[:1500]
            linked = p.get("url") or ""
            if fetchers.arxiv_id_or_none(linked):
                # linkposts: ingest the underlying paper, keep the post's karma as the signal
                new += add_candidate(conn, url=linked, title=p["title"], source="arxiv",
                                     published_date=p["postedAt"][:10], authors=authors, abstract=snippet,
                                     signal=p["baseScore"], reason="LessWrong linkpost")
            new += add_candidate(conn, url=f"https://www.lesswrong.com/posts/{p['_id']}", title=p["title"],
                                 source="lesswrong", published_date=p["postedAt"][:10], authors=authors,
                                 abstract=snippet, signal=p["baseScore"],
                                 reason=f"{'AF' if p.get('af') else 'LW'} interpretability tag, karma {p['baseScore']}")
        conn.commit()
        if len(posts) < 300:
            break
        before = posts[-1]["postedAt"]
    _set_state(conn, "discover.lesswrong", datetime.now(timezone.utc).isoformat())
    return new


def discover_transformer_circuits(conn, cfg: dict) -> int:
    r = _client.get("https://transformer-circuits.pub/")
    r.raise_for_status()
    new = 0
    for a in BeautifulSoup(r.text, "html.parser").find_all("a", href=True):
        href = a["href"].lstrip("./")
        if not re.match(r"^20\d\d/[^/]+/(index\.html)?$", href):
            continue
        # Index cards also contain author bylines and descriptive blurbs. Only
        # the heading is the document title used for identity verification.
        heading = a.select_one("h1, h2, h3, h4, h5, h6, .title")
        title = " ".join((heading if heading is not None else a).get_text(" ").split()) or href
        new += add_candidate(conn, url=f"https://transformer-circuits.pub/{href}", title=title,
                             source="transformer_circuits", published_date=href[:4],
                             reason="transformer-circuits.pub index")
    conn.commit()
    return new


def discover_citation_stubs(conn, cfg: dict) -> int:
    """Older foundational work enters the map by being cited by what we've ingested."""
    min_n = cfg["discovery"]["citation_stubs"]["min_citations"]
    new = 0
    for r in conn.execute(
            """SELECT d.title, d.url, d.arxiv_id, COUNT(*) AS n FROM document_links l
               JOIN documents d ON d.id = l.cited_id
               WHERE d.status = 'stub' AND l.link_type = 'explicit_citation'
                 AND (d.url IS NOT NULL OR d.arxiv_id IS NOT NULL)
               GROUP BY d.id HAVING n >= ?""", (min_n,)).fetchall():
        url = r["url"] or f"https://arxiv.org/abs/{r['arxiv_id']}"
        if "semanticscholar.org" in url:
            continue  # no fetchable full text behind these
        new += add_candidate(conn, url=url, title=r["title"], source="citation", signal=r["n"],
                             reason=f"cited by {r['n']} ingested documents")
        conn.execute("UPDATE candidates SET signal = ? WHERE url = ?", (r["n"], db.normalize_url(url)))
    conn.commit()
    return new


DISCOVERERS = {
    "arxiv": discover_arxiv,
    "lesswrong": discover_lesswrong,
    "transformer_circuits": discover_transformer_circuits,
    "citation_stubs": discover_citation_stubs,
}


def discover(conn, cfg: dict) -> dict:
    counts = {}
    for name, fn in DISCOVERERS.items():
        if not cfg["discovery"][name]["enabled"]:
            continue
        try:
            counts[name] = fn(conn, cfg)
            log(f"  discover {name}: {counts[name]} new candidates")
        except Exception as e:  # one broken source shouldn't stop the run
            counts[name] = f"error: {e}"
            log(f"  discover {name}: FAILED {e}")
    return counts


# ---------------------------------------------------------------- triage

TRIAGE_INSTRUCTIONS = """\
You are triaging candidate documents for a research map of LLM mechanistic interpretability
and interpretability-relevant alignment work. Rate each item's relevance:

3 = core: new interpretability methods, circuits, features / SAEs / transcoders, probing or
    steering of model internals with findings of general interest, superposition, causal
    analysis of LLM internals, interpretability-based alignment or safety research, or a
    substantive critique / replication of such work. Important persona and character
    research is also core: learned persona representations, persona selection or drift,
    role-play, character training, and emergent misalignment, including their influential
    conceptual foundations. Papers and substantive research blog posts are both eligible;
    lack of new experiments alone does not make a conceptual foundation tangential.
2 = relevant: applies interpretability methods to language models with findings that bear
    on the field's claims, or alignment research with meaningful interpretability content.
1 = tangential: interpretability tools applied to a narrow domain or non-language models,
    benchmarks, opinion pieces with little technical content.
0 = not relevant.

Return a JSON object {"items": [{"id": <id>, "relevance": <0-3>, "note": "<= 12 words why"}]}
covering every item, and nothing else.
"""

TRIAGE_SCHEMA = {
    "type": "object", "properties": {"items": {"type": "array", "items": {
        "type": "object", "properties": {
            "id": {"type": "integer"},
            "relevance": {"type": "integer", "enum": [0, 1, 2, 3]},
            "note": {"type": "string"},
        }, "required": ["id", "relevance", "note"], "additionalProperties": False,
    }}}, "required": ["items"], "additionalProperties": False,
}


def triage(conn, cfg: dict, *, citations_only: int = 0) -> int:
    """Score untriaged candidates in batches with one headless call per batch. citations_only=n
    restricts this pass to citation candidates cited at least n times by the map."""
    # Preserve the existing Claude triage for API updates; Codex updates use Codex throughout.
    backend = cfg.get("triage", {}).get("backend") or (
        "codex_cli" if cfg["update"]["backend"] == "codex_cli" else "claude_cli")
    if backend not in ("claude_cli", "codex_cli"):
        raise ValueError(f"unknown triage backend: {backend}")
    rows = conn.execute("""SELECT id, title, source, abstract, reason FROM candidates
                           WHERE status = 'new' AND relevance IS NULL ORDER BY id""").fetchall()
    # Transformer Circuits publishes only interpretability work; no need to spend a call.
    # The monthly "Circuits Updates" notes are short, so they rank below full papers.
    tc = [r for r in rows if r["source"] == "transformer_circuits"]
    conn.executemany("UPDATE candidates SET relevance = ?, triage_note = ? WHERE id = ?",
                     [(2, "transformer-circuits.pub monthly update", r["id"]) if "updates" in r["title"].lower()
                      else (3, "transformer-circuits.pub", r["id"]) for r in tc])
    conn.commit()
    rows = [r for r in rows if r["source"] != "transformer_circuits"]
    if citations_only:
        signal = dict(conn.execute("SELECT id, signal FROM candidates WHERE status = 'new'").fetchall())
        rows = [r for r in rows if r["source"] == "citation" and (signal.get(r["id"]) or 0) >= citations_only]
    size, scored = cfg["triage"]["batch_size"], len(tc)
    for i in range(0, len(rows), size):
        batch = rows[i:i + size]
        items = [{"id": r["id"], "title": r["title"], "source": r["source"], "context": r["reason"],
                  "abstract": (r["abstract"] or "")[:1200]} for r in batch]
        prompt = f"{TRIAGE_INSTRUCTIONS}\n<items>\n{json.dumps(items, indent=1)}\n</items>\n"
        raw = (extract.run_codex_cli(prompt, TRIAGE_SCHEMA) if backend == "codex_cli"
               else extract.run_claude_cli(prompt))
        m = re.search(r"\{.*\}", raw, re.S)
        results = json.loads(m.group(0))["items"] if m else []
        valid = {r["id"] for r in batch}
        for it in results:
            if it.get("id") in valid and isinstance(it.get("relevance"), int):
                conn.execute("UPDATE candidates SET relevance = ?, triage_note = ? WHERE id = ?",
                             (max(0, min(3, it["relevance"])), str(it.get("note", ""))[:200], it["id"]))
                scored += 1
        conn.commit()
        log(f"  triage batch {i // size + 1}: scored {len(results)} of {len(batch)}")
    return scored


def priority(row) -> float:
    """Ordering among relevant candidates: relevance first, then karma / citation signal."""
    manual = 100 if row["status"] == "accepted" else 0
    return manual + row["relevance"] * 10 + math.log1p(row["signal"] or 0)


def select_candidates(conn, cfg: dict, limit: int) -> list:
    min_rel = cfg["triage"]["min_relevance"]
    conn.execute("""UPDATE candidates SET status = 'rejected', decided_at = datetime('now')
                    WHERE status = 'new' AND relevance IS NOT NULL AND relevance < ?""", (min_rel,))
    conn.commit()
    rows = conn.execute("SELECT * FROM candidates WHERE status IN ('new', 'accepted') AND relevance >= ?",
                        (min_rel,)).fetchall()
    # hand-accepted candidates (canon, `cli.py accept`) go first; the rest round-robin across
    # sources so one large backlog (e.g. the Transformer Circuits archive on the first run)
    # doesn't crowd out new arXiv papers and posts
    chosen = sorted((r for r in rows if r["status"] == "accepted"), key=priority, reverse=True)[:limit]
    queues: dict[str, list] = {}
    for r in sorted((r for r in rows if r["status"] != "accepted"), key=priority, reverse=True):
        queues.setdefault(r["source"], []).append(r)
    order = sorted(queues, key=lambda src: -priority(queues[src][0]))
    while len(chosen) < limit and any(queues.values()):
        for src in order:
            if queues[src] and len(chosen) < limit:
                chosen.append(queues[src].pop(0))
    return chosen


def title_matches(expected: list[str], actual: str) -> bool:
    """Guard against a wrong URL or ID silently ingesting the wrong paper."""
    from difflib import SequenceMatcher
    norm = lambda t: re.sub(r"[^a-z0-9 ]", "", t.lower()).strip()  # noqa: E731
    a = norm(actual)
    return any(e and (norm(e) in a or SequenceMatcher(None, norm(e), a).ratio() >= 0.75) for e in map(norm, expected))


def add_canon(conn, items: list[dict]) -> int:
    """Hand-picked foundational works: accepted directly, verified by title when fetched."""
    added = 0
    for it in items:
        expected = [it["title"], *it.get("also", [])]
        if add_candidate(conn, url=it["url"], title=it["title"], source="canon",
                         reason="canon: " + " || ".join(expected)):
            added += 1
        conn.execute("UPDATE candidates SET status = 'accepted', relevance = 3 WHERE url = ? AND status = 'new'",
                     (db.normalize_url(it["url"]),))
    conn.commit()
    return added


def ingest_candidates(conn, rows) -> list[int]:
    doc_ids = []
    for r in rows:
        try:
            f = fetchers.fetch(r["url"])
            if (r["reason"] or "").startswith("canon: "):
                expected = r["reason"][len("canon: "):].split(" || ")
                if not title_matches(expected, f.meta.title):
                    raise ValueError(f"title mismatch: expected {expected[0]!r}, got {f.meta.title!r}")
            doc_id = store_fetched(conn, f)
            conn.execute("UPDATE candidates SET status = 'ingested', document_id = ?, decided_at = datetime('now') "
                         "WHERE id = ?", (doc_id, r["id"]))
            doc_ids.append(doc_id)
            log(f"  fetched [{f.meta.source}] {f.meta.title[:80]}  ({len(f.text):,} chars)")
        except Exception as e:
            conn.execute("UPDATE candidates SET status = 'failed', triage_note = ? WHERE id = ?",
                         (f"fetch failed: {e}"[:200], r["id"]))
            log(f"  fetch FAILED {r['url']}: {e}")
        conn.commit()
    return doc_ids


def resolve_stub_titles(conn) -> int:
    """Stubs come from link text or bare URLs ("[12]", "this paper", "https://arxiv.org/abs/...");
    look up real titles, authors and dates for the arXiv ones in batches."""
    rows = conn.execute("SELECT id, arxiv_id, title FROM documents WHERE status = 'stub' AND arxiv_id IS NOT NULL").fetchall()
    weak = [r for r in rows if db.title_quality(r["title"]) < 30 or "arxiv.org" in r["title"]]
    if not weak:
        return 0
    meta = fetchers.arxiv_metadata([r["arxiv_id"] for r in weak])
    fixed = 0
    for r in weak:
        m = meta.get(r["arxiv_id"])
        if not m:
            continue
        conn.execute("UPDATE documents SET title = ?, published_date = COALESCE(published_date, ?) WHERE id = ?",
                     (m["title"], m["published_date"], r["id"]))
        db.set_authors(conn, r["id"], m["authors"])
        fixed += 1
    conn.commit()
    return fixed


# ---------------------------------------------------------------- parallel extraction

def vocabulary_for(conn, doc_id, f: Fetched) -> dict:
    """The labels worth showing the extractor for one document (see db.relevant_vocabulary),
    plus any claims it was specifically selected to test (see gaps.py)."""
    vocab = db.relevant_vocabulary(conn, doc_id, f.meta.title, f.meta.abstract or "", f.text or "")
    if doc_id is not None:
        from . import gaps
        focus = gaps.focus_claims_for(conn, doc_id)
        if focus:
            vocab["focus_claims"] = focus
    return vocab


def pending_documents(conn, limit: Optional[int] = None) -> list:
    rows = conn.execute(
        """SELECT d.* FROM documents d WHERE d.status = 'fetched'
           AND (SELECT COUNT(*) FROM extraction_failures f WHERE f.document_id = d.id) < ?
           ORDER BY d.created_at""", (MAX_EXTRACTION_FAILURES,)).fetchall()
    return rows if limit is None else rows[:limit]


def extract_pending(conn, *, workers: int, wave_size: int, limit: Optional[int] = None,
                    backend: str = "claude_cli", document_ids: Optional[list[int]] = None) -> dict:
    """Extract waiting documents in waves. Within a wave, extractions run in parallel against
    the same vocabulary snapshot; ingestion is serial. Each wave sees the previous wave's
    new concepts and claims, which keeps labels consistent."""
    run = extract.extraction_backend(backend)
    if workers < 1 or wave_size < 1 or (limit is not None and limit < 0):
        raise ValueError("workers and wave_size must be positive; limit must be nonnegative")
    docs = pending_documents(conn)
    if document_ids is not None:
        rank = {doc_id: i for i, doc_id in enumerate(dict.fromkeys(document_ids))}
        docs = sorted((d for d in docs if d["id"] in rank), key=lambda d: rank[d["id"]])
    if limit is not None:
        docs = docs[:limit]
    stats = {"extracted": 0, "failed": 0, "stopped": None, "waves": 0}
    extract.EXTRACTIONS_DIR.mkdir(parents=True, exist_ok=True)
    for w in range(0, len(docs), wave_size):
        wave = docs[w:w + wave_size]
        stats["waves"] += 1
        total_claims = conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
        log(f"  wave {stats['waves']}: {len(wave)} documents, {workers} workers, "
            f"{total_claims} claims in the map (each document sees the relevant ones)")
        jobs = []
        for d in wave:  # SQLite work stays on this thread; workers only run the model
            f = fetched_from_db(conn, d)
            jobs.append((d, f, vocabulary_for(conn, d["id"], f)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(run, f, vocab): d for d, f, vocab in jobs}
            for fut in as_completed(futures):
                d = futures[fut]
                try:
                    ex = fut.result()
                except CancelledError:
                    continue  # cancelled work remains pending, not a document failure
                except (extract.UsageLimitError, extract.NotLoggedInError) as e:
                    stats["stopped"] = f"{type(e).__name__}: {e}"
                    for other in futures:
                        other.cancel()  # not-yet-started jobs; running ones finish
                    log(f"  STOP: {stats['stopped']}")
                    continue
                except Exception as e:
                    conn.execute("INSERT INTO extraction_failures (document_id, error) VALUES (?, ?)",
                                 (d["id"], str(e)[:1000]))
                    conn.commit()
                    stats["failed"] += 1
                    log(f"  extraction FAILED {d['slug']}: {str(e)[:200]}")
                    continue
                (extract.EXTRACTIONS_DIR / f"{d['slug']}.json").write_text(ex.model_dump_json(indent=2),
                                                                            encoding="utf-8")
                ex.document = fetched_from_db(conn, d).meta  # fetched metadata is authoritative
                db.ingest_extraction(conn, ex)
                stats["extracted"] += 1
                log(f"  extracted {d['slug']}: {len(ex.concepts)} concepts, {len(ex.claims)} claims")
        if stats["stopped"]:
            break
    return stats


# ---------------------------------------------------------------- the scheduled update

class _Lock:
    """Prevents two update runs at once (e.g. a slow run overlapping the next schedule)."""

    def __enter__(self):
        if LOCK_PATH.exists() and time.time() - LOCK_PATH.stat().st_mtime < 12 * 3600:
            raise RuntimeError(f"another update appears to be running ({LOCK_PATH}); delete it if stale")
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        LOCK_PATH.write_text(str(os.getpid()))
        return self

    def __exit__(self, *exc):
        LOCK_PATH.unlink(missing_ok=True)


def update(conn, cfg: dict, *, skip_discovery: bool = False, skip_triage: bool = False,
           dry_run: bool = False) -> dict:
    u = cfg["update"]
    started = datetime.now()
    summary: dict = {"started": started.isoformat(timespec="seconds")}
    with _Lock():
        before = {c.slug: c.status for c in report.claim_health(conn).values()}
        log("== discover")
        summary["discovered"] = {} if skip_discovery else discover(conn, cfg)
        log("== triage")
        try:
            summary["triaged"] = {} if skip_triage else triage(conn, cfg)
        except (extract.UsageLimitError, extract.NotLoggedInError) as e:
            summary["stopped"] = f"triage: {type(e).__name__}: {e}"
            log(f"  STOP: {summary['stopped']}")
        room = max(0, u["max_new_documents_per_run"] - len(pending_documents(conn)))
        # a run that can't reach its provider can't extract either
        chosen = select_candidates(conn, cfg, room) if dry_run or "stopped" not in summary else []
        summary["selected"] = [{"title": r["title"], "source": r["source"], "relevance": r["relevance"],
                                "note": r["triage_note"]} for r in chosen]
        log(f"== selected {len(chosen)} candidates (room for {room})")
        for r in chosen:
            log(f"  [{r['relevance']}] {r['source']:20} {r['title'][:80]}")
        if dry_run:
            return summary
        if "stopped" not in summary:
            log("== fetch")
            summary["fetched"] = len(ingest_candidates(conn, chosen))
            try:
                summary["stub_titles_resolved"] = resolve_stub_titles(conn)
            except Exception as e:  # nice to have; never block extraction on it
                log(f"  resolving stub titles failed: {e}")
            log("== extract")
            summary["extraction"] = extract_pending(conn, workers=u["workers"], wave_size=u["wave_size"],
                                                    limit=u["max_new_documents_per_run"], backend=u["backend"])
            if summary["extraction"]["stopped"]:
                summary["stopped"] = summary["extraction"]["stopped"]
        after = report.claim_health(conn)
        # existing claims whose status moved are the interesting signal; new claims are listed apart
        summary["status_changes"] = [
            {"claim": c.slug, "from": before[c.slug], "to": c.status}
            for c in after.values() if c.slug in before and before[c.slug] != c.status]
        summary["new_claims"] = [{"claim": c.slug, "status": c.status}
                                 for c in after.values() if c.slug not in before]
        summary["vocab_duplicates"] = [
            {"kind": k, "a": a, "b": b, "score": round(s, 2)} for s, k, a, b in db.possible_vocab_duplicates(conn)]
        summary["finished"] = datetime.now().isoformat(timespec="seconds")
        conn.execute("INSERT INTO update_runs (started_at, finished_at, summary) VALUES (?, ?, ?)",
                     (summary["started"], summary["finished"], json.dumps(summary)))
        conn.commit()
        write_run_log(summary)
    return summary


def write_run_log(s: dict) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"update-{s['started'].replace(':', '').replace('-', '')[:13]}.md"
    ex = s.get("extraction") or {}
    lines = [f"# Update run {s['started']}", ""]
    if s.get("stopped"):
        lines += [f"**Stopped early:** {s['stopped']}", ""]
    lines += ["## Discovery", *[f"- {k}: {v}" for k, v in s.get("discovered", {}).items()], "",
              f"## Selected ({len(s.get('selected', []))})",
              *[f"- [{r['relevance']}] {r['title']} ({r['source']}) - {r['note'] or ''}" for r in s.get("selected", [])], "",
              "## Extraction",
              f"- fetched {s.get('fetched', 0)}, extracted {ex.get('extracted', 0)}, failed {ex.get('failed', 0)}, "
              f"waves {ex.get('waves', 0)}", "",
              "## Status changes on existing claims",
              *([f"- {c['claim']}: {c['from']} -> {c['to']}" for c in s.get("status_changes", [])] or ["- none"]), "",
              f"## New claims ({len(s.get('new_claims', []))})",
              *[f"- [{c['status']}] {c['claim']}" for c in s.get("new_claims", [])], "",
              "## Possible duplicate labels (review with `cli.py vocab-dupes`, fix with `cli.py merge`)",
              *([f"- {d['kind']}: {d['a']} ~ {d['b']} ({d['score']})" for d in s.get("vocab_duplicates", [])] or ["- none"])]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
