"""Consolidating claims: folding paper-specific results into broader claims and merging
duplicates, so evidence from different papers lands on the same claim.

Two stages, with a person in between:

1. `plan()` groups claims by concept into batches and asks a model, per batch, which claims
   are duplicates of one another and which are specific instances of a broader claim
   (existing, or a new statement it proposes). The result is saved as a plan file; nothing
   in the database changes.
2. `apply_groups()` applies the groups a reviewer accepted:
   - duplicate: merged into the target (evidence, dependencies and notes move; the old
     slug becomes an alias, so future extractions using it land on the target);
   - instance: its evidence moves to the target with the specific finding written into
     the evidence description, then the narrow claim is retired as an alias.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

from . import curation, db, extract

PLANS_DIR = db.ROOT / "data" / "consolidation"
BATCH_SIZE = 140


class Member(BaseModel):
    id: str = Field(description="claim id from the list, e.g. c17")
    kind: Literal["duplicate", "instance"]


class Group(BaseModel):
    # short ids (c17 / h3) instead of slugs keep the answer small; they're mapped back afterwards
    target: Optional[str] = Field(None, description="id of an existing claim (c.. or h..) to fold members into, or null to propose a new claim")
    new_statement: Optional[str] = Field(None, description="when target is null: the broader, testable claim the members are instances of")
    new_claim_type: Optional[Literal["empirical", "theoretical", "methodological", "hypothesis"]] = None
    members: list[Member]
    why: str = Field(description="at most 15 words: why these belong together")


class BatchResult(BaseModel):
    groups: list[Group]


INSTRUCTIONS = """\
You are consolidating the claims in a research map of LLM interpretability. The map's goal is
for evidence from different papers to land on the SAME claim, so the field can see which
claims are replicated, contested or untested. Many existing claims are too narrow: they
restate one paper's specific result instead of a claim other work could test or rely on.

Below are claims from one area. Each line gives id | number of documents with evidence |
statement. Ids starting with "h" are broader claims (already widely shared) that you may use
as targets; ids starting with "c" are the claims to consolidate. Refer to claims only by id.

Return groups. Each group has a target and members:
- target: the id of an existing claim (from either list) that the members should fold into,
  or, if no existing claim captures them, a NEW broader statement (target null,
  new_statement set) that other work could independently test or rely on.
- member kind "duplicate": says the same thing as the target (rephrasing). It is merged.
- member kind "instance": a specific result, setting or case of the target (particular
  model, dataset, metric, number). It becomes evidence on the target; its text is kept in
  the evidence description, so nothing is lost.

Rules:
- Only group claims that are genuinely about the same thing. Claims that differ in substance,
  direction (one asserts X, the other not-X), or mechanism must NOT be merged; a result that
  contradicts the target is not an instance of it.
- A new target must be broader than every member, testable, and phrased to stand alone.
  Propose a new target only when at least two members share it.
- Prefer targets with more documents. Never make a claim a member of more than one group.
- Leave claims that are already appropriately general and distinct out of every group.
- Methodological claims ("X provides a useful baseline/proxy") fold into a broader
  methodological claim about that method when one exists or is warranted.

Keep "why" to 15 words or fewer. Output a JSON object matching the schema, and nothing else.
"""


def batches(conn) -> list[dict]:
    """Batches of related claims, ~BATCH_SIZE each.

    Concepts are fine-grained, so batching by a claim's own concept would rarely put related
    claims together. Instead each claim goes to the broadest ancestor concept whose whole
    subtree still fits in one batch; small subtrees under the same parent are then packed
    together. Widely shared claims anywhere in a batch's subtrees are offered as targets.
    """
    from .report import concept_descendants

    claims = {r["id"]: dict(r) for r in conn.execute("SELECT id, slug, statement FROM claims")}
    docs = dict(conn.execute("SELECT claim_id, COUNT(DISTINCT document_id) FROM claim_evidence GROUP BY claim_id"))
    names = dict(conn.execute("SELECT id, name FROM concepts"))
    parents = defaultdict(set)
    for child, parent in conn.execute("SELECT child_id, parent_id FROM concept_relations"):
        parents[child].add(parent)
    tagged = defaultdict(set)   # concept -> claims tagged directly
    claim_concepts = defaultdict(set)
    for claim_id, concept_id in conn.execute("SELECT claim_id, concept_id FROM claim_concepts"):
        tagged[concept_id].add(claim_id)
        claim_concepts[claim_id].add(concept_id)
    desc = concept_descendants(conn)
    subtree = {c: set().union(*(tagged[d] for d in ds)) for c, ds in desc.items()}

    def ancestors(c):
        seen, stack = {c}, [c]
        while stack:
            for p in parents[stack.pop()]:
                if p not in seen:
                    seen.add(p)
                    stack.append(p)
        return seen

    home = {}
    for claim_id in claims:
        own = claim_concepts[claim_id]
        cands = set().union(*(ancestors(c) for c in own)) if own else set()
        fitting = [c for c in cands if len(subtree[c]) <= BATCH_SIZE]
        if fitting:
            home[claim_id] = max(fitting, key=lambda c: (len(subtree[c]), -c))
        elif own:  # even its own concept is huge: use the smallest, and chunk it
            home[claim_id] = min(own, key=lambda c: (len(subtree[c]), c))
        else:
            home[claim_id] = None
    groups = defaultdict(list)
    for claim_id, c in home.items():
        groups[c].append(claim_id)

    # big groups become their own batches; small ones sharing a parent are packed together
    bins: list[tuple[list[int], list[int]]] = []   # (concept ids, claim ids)
    small = defaultdict(list)
    for c, ids in groups.items():
        if c is None or len(ids) >= BATCH_SIZE // 3:
            ordered = sorted(ids, key=lambda i: claims[i]["slug"])
            for k in range(0, len(ordered), BATCH_SIZE):
                bins.append(([c] if c else [], ordered[k:k + BATCH_SIZE]))
        else:
            small[min(parents[c]) if parents[c] else None].append(c)
    leftovers = []
    for _, cs in sorted(small.items(), key=lambda kv: str(kv[0])):
        cur_c, cur = [], []
        for c in sorted(cs, key=lambda c: -len(groups[c])):
            if cur and len(cur) + len(groups[c]) > BATCH_SIZE:
                bins.append((cur_c, cur))
                cur_c, cur = [], []
            cur_c.append(c)
            cur += groups[c]
        if cur:
            leftovers.append((cur_c, cur))
    # siblings-of-siblings: pack the remaining small bins together rather than sending tiny batches
    cur_c, cur = [], []
    for cs, ids in sorted(leftovers, key=lambda b: -len(b[1])):
        if cur and len(cur) + len(ids) > BATCH_SIZE:
            bins.append((cur_c, cur))
            cur_c, cur = [], []
        cur_c += cs
        cur += ids
    if cur:
        bins.append((cur_c, cur))

    out = []
    for cs, ids in bins:
        chunk = set(ids)
        area = set().union(*(subtree[c] for c in cs)) if cs else set()
        hubs = sorted((i for i in area if i not in chunk and docs.get(i, 0) >= 3), key=lambda i: -docs.get(i, 0))[:25]
        label = ", ".join(names[c] for c in cs[:4]) + (f" +{len(cs) - 4} more" if len(cs) > 4 else "")
        out.append({
            "concept": label or "(no concept)",
            "claims": [{"slug": claims[i]["slug"], "documents": docs.get(i, 0), "statement": claims[i]["statement"]} for i in ids],
            "hubs": [{"slug": claims[i]["slug"], "documents": docs.get(i, 0), "statement": claims[i]["statement"]} for i in hubs],
        })
    return sorted(out, key=lambda b: -len(b["claims"]))


def _ids(batch: dict) -> dict[str, dict]:
    """Short ids for the prompt: c1.. for claims to consolidate, h1.. for available targets."""
    return {**{f"c{i}": c for i, c in enumerate(batch["claims"], 1)},
            **{f"h{i}": c for i, c in enumerate(batch["hubs"], 1)}}


def _prompt(batch: dict) -> str:
    ids = _ids(batch)

    def lines(prefix):
        return "\n".join(f"{k} | {c['documents']} | {c['statement']}" for k, c in ids.items() if k[0] == prefix)
    # stable instructions and schema first, so repeated calls can reuse the cached prefix
    return (f"{INSTRUCTIONS}\n<schema>\n{json.dumps(BatchResult.model_json_schema())}\n</schema>\n\n"
            f"<area>{batch['concept']}</area>\n\n<claims>\n{lines('c')}\n</claims>\n\n"
            f"<broader_claims_available_as_targets>\n{lines('h') or '(none)'}\n</broader_claims_available_as_targets>\n")


def _run_model(prompt: str, backend: str, telemetry: Optional[dict] = None) -> BatchResult:
    for attempt in range(2):
        if backend == "codex_cli":
            model, effort = extract.codex_settings()
            raw = extract.run_codex_cli(prompt, BatchResult.model_json_schema(), model=model,
                                        reasoning_effort=effort, telemetry=telemetry)
        else:
            raw = extract.run_claude_cli(prompt, telemetry=telemetry)
        m = re.search(r"\{.*\}", raw, re.S)
        try:
            return BatchResult.model_validate_json(m.group(0) if m else raw)
        except ValueError as e:
            prompt += f"\n\nYour previous output was invalid ({str(e)[:2000]}). Return only the corrected JSON."
    raise RuntimeError("consolidation output failed validation twice")


def validate(batch: dict, result: BatchResult) -> list[dict]:
    """Map short ids back to claims, and drop anything the model made up or got structurally wrong."""
    ids = _ids(batch)
    in_batch = {c["slug"]: c for c in batch["claims"]}
    used, groups = set(), []
    for g in result.groups:
        t = (g.target or "").strip().lower()
        target = ids[t] if t in ids else None
        if t and not target:
            continue  # unknown target id
        if not target and not (g.new_statement and g.new_statement.strip()):
            continue
        members = []
        for m in g.members:
            key = m.id.strip().lower()
            if not key.startswith("c") or key not in ids:
                continue  # only claims from this batch can be folded
            claim = ids[key]
            if claim["slug"] in used or (target and claim["slug"] == target["slug"]):
                continue
            members.append((claim, m.kind))
            used.add(claim["slug"])
        if not members or (not target and len(members) < 2):
            continue
        groups.append({
            "target_slug": target["slug"] if target else None,
            "target_statement": target["statement"] if target else g.new_statement.strip(),
            "target_documents": target["documents"] if target else 0,
            "new_claim_type": None if target else (g.new_claim_type or "empirical"),
            "members": [{"slug": c["slug"], "kind": kind, "statement": c["statement"], "documents": c["documents"]}
                        for c, kind in members],
            "rationale": g.why,
            "concept": batch["concept"],
        })
    return groups


def batch_key(batch: dict) -> str:
    import hashlib
    return hashlib.sha1("\n".join(sorted(c["slug"] for c in batch["claims"])).encode()).hexdigest()[:16]


def plan(conn, *, backend: str = "claude_cli", limit_batches: Optional[int] = None, workers: int = 4,
         resume: bool = False, log=print) -> Path:
    """Model calls run in parallel; results are collected here so the plan file stays consistent.
    The file is rewritten after every batch, so a partial plan is usable if the run stops, and
    `resume` continues it (finished batches are skipped) instead of paying for them again."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    all_batches = batches(conn)[:limit_batches]
    previous = latest_plan() if resume else None
    if previous and json.loads(previous.read_text(encoding="utf-8")).get("status") == "partial":
        path = previous
        record = json.loads(path.read_text(encoding="utf-8"))
        record.pop("stopped", None)
        record["backend"] = backend
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        path = PLANS_DIR / f"plan-{stamp}.json"
        record = {"created": stamp, "backend": backend, "groups": [], "status": "partial"}
    finished = set(record.setdefault("done_batch_keys", []))
    already = {m["slug"] for g in record["groups"] for m in g["members"]}
    for b in all_batches:
        b["key"] = batch_key(b)  # before any filtering, so keys stay stable across resumes
    todo = [b for b in all_batches if b["key"] not in finished]
    for b in todo:  # claims already folded by an earlier batch of this plan stay out
        b["claims"] = [c for c in b["claims"] if c["slug"] not in already]
    record["batches"] = len(all_batches)
    log(f"  {len(all_batches) - len(todo)} batches already done; {len(todo)} to go")
    done, stop = len(all_batches) - len(todo), False
    usage = record.setdefault("usage", {"calls": 0, "input": 0, "cache_write": 0, "cache_read": 0, "output": 0})

    def call(b):
        tel = {"purpose": "consolidate"}
        result = _run_model(_prompt(b), backend, telemetry=tel)
        return result, tel

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(call, b): b for b in todo if b["claims"]}
        for fut in as_completed(futures):
            b = futures[fut]
            try:
                result, tel = fut.result()
                groups = validate(b, result)
                usage["calls"] += 1
                for k in ("input", "cache_write", "cache_read", "output"):
                    usage[k] += tel.get(k, 0) or 0
            except (extract.UsageLimitError, extract.NotLoggedInError) as e:
                if not stop:
                    record["stopped"] = f"{type(e).__name__}: {e}"
                    log(f"  STOP: {record['stopped']}")
                    stop = True
                    for other in futures:
                        other.cancel()
                continue
            except Exception as e:  # one bad batch shouldn't sink the plan
                if not fut.cancelled():
                    log(f"  batch ({b['concept'][:40]}) failed: {str(e)[:200]}")
                continue
            done += 1
            record["done_batch_keys"].append(b["key"])
            for g in groups:
                g["id"] = len(record["groups"])
                g["status"] = "pending"
                record["groups"].append(g)
            log(f"  batch {done}/{len(all_batches)} {b['concept'][:40]}: {len(b['claims'])} claims -> {len(groups)} groups, "
                f"{sum(len(g['members']) for g in groups)} claims folded; {tel.get('output', 0)} output tokens")
            path.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    if not stop and done == len(all_batches):
        record["status"] = "ready"
    record["batches_done"] = done
    path.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def latest_plan() -> Optional[Path]:
    plans = sorted(PLANS_DIR.glob("plan-*.json")) if PLANS_DIR.exists() else []
    return plans[-1] if plans else None


# ---------------------------------------------------------------- applying

def _demote(conn, member_slug: str, target_slug: str) -> int:
    """Fold a narrow claim into a broader one as evidence, then retire it as an alias."""
    src = conn.execute("SELECT id, statement FROM claims WHERE slug = ?", (member_slug,)).fetchone()
    dst = conn.execute("SELECT id FROM claims WHERE slug = ?", (target_slug,)).fetchone()
    if not src or not dst:
        raise curation.CurationError(f"missing claim: {member_slug if not src else target_slug}")
    moved = 0
    for e in conn.execute("SELECT * FROM claim_evidence WHERE claim_id = ?", (src["id"],)).fetchall():
        # on the broader claim, a paper that "proposed" a specific tested result is supporting evidence
        relation = e["relation"]
        if relation == "proposes":
            relation = "supports" if e["strength"] else "extends"
        note = f"Specific finding: {src['statement']}" + (f" {e['experiment_summary']}" if e["experiment_summary"] else "")
        clash = conn.execute("SELECT id, experiment_summary FROM claim_evidence WHERE claim_id = ? AND document_id = ? AND relation = ?",
                             (dst["id"], e["document_id"], relation)).fetchone()
        if clash:
            conn.execute("UPDATE claim_evidence SET experiment_summary = ? WHERE id = ?",
                         (((clash["experiment_summary"] or "") + "\n" + note).strip(), clash["id"]))
        else:
            conn.execute("""INSERT INTO claim_evidence (claim_id, document_id, relation, strength, experiment_summary,
                            models_studied, location, provenance, reviewed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                         (dst["id"], e["document_id"], relation, e["strength"], note, e["models_studied"],
                          e["location"], (e["provenance"] or "") + " +consolidated", e["reviewed"]))
        moved += 1
    conn.execute("DELETE FROM claim_evidence WHERE claim_id = ?", (src["id"],))
    # concepts and dependencies carry over; the claim itself becomes an alias
    conn.execute("INSERT OR IGNORE INTO claim_concepts SELECT ?, concept_id FROM claim_concepts WHERE claim_id = ?", (dst["id"], src["id"]))
    conn.execute("UPDATE OR IGNORE claim_dependencies SET claim_id = ? WHERE claim_id = ?", (dst["id"], src["id"]))
    conn.execute("UPDATE OR IGNORE claim_dependencies SET depends_on_id = ? WHERE depends_on_id = ?", (dst["id"], src["id"]))
    conn.execute("UPDATE annotations SET target_id = ? WHERE target_type = 'claim' AND target_id = ?", (dst["id"], src["id"]))
    conn.execute("DELETE FROM claims WHERE id = ?", (src["id"],))
    conn.execute("DELETE FROM claim_dependencies WHERE claim_id = depends_on_id")
    db.break_dependency_cycles(conn)  # re-pointed dependencies can close a loop
    conn.execute("UPDATE vocab_aliases SET new_slug = ? WHERE kind = 'claim' AND new_slug = ?", (target_slug, member_slug))
    conn.execute("INSERT OR REPLACE INTO vocab_aliases VALUES ('claim', ?, ?)", (member_slug, target_slug))
    return moved


def apply_groups(conn, path: Path, group_ids: list[int], user: str = "me") -> dict:
    record = json.loads(path.read_text(encoding="utf-8"))
    by_id = {g["id"]: g for g in record["groups"]}
    applied, skipped = 0, []
    for gid in group_ids:
        g = by_id.get(gid)
        if not g or g["status"] != "pending":
            continue
        live = [m for m in g["members"] if conn.execute("SELECT 1 FROM claims WHERE slug = ?", (m["slug"],)).fetchone()]
        if not live:  # everything was folded or merged elsewhere since the plan was made
            g["status"] = "skipped"
            continue
        try:
            target = g["target_slug"]
            if target and not conn.execute("SELECT 1 FROM claims WHERE slug = ?", (target,)).fetchone():
                target = db.resolve_slug(conn, "claim", target)  # merged since the plan was made
            if not target:
                target = curation.create_claim(conn, statement=g["target_statement"],
                                               claim_type=g["new_claim_type"] or "empirical")
            moved = 0
            for m in g["members"]:
                if not conn.execute("SELECT 1 FROM claims WHERE slug = ?", (m["slug"],)).fetchone() or m["slug"] == target:
                    continue  # already folded elsewhere
                if m["kind"] == "duplicate":
                    curation.merge(conn, "claim", m["slug"], target)
                else:
                    moved += _demote(conn, m["slug"], target)
            curation.log(conn, "consolidate", "claim", target,
                         {"members": [[m["slug"], m["kind"]] for m in g["members"]], "evidence_moved": moved,
                          "plan": path.name, "group": gid}, user=user)
            conn.commit()
            g["status"], g["applied_target"] = "applied", target
            applied += 1
        except curation.CurationError as e:
            conn.rollback()
            g["status"], g["error"] = "failed", str(e)
            skipped.append(gid)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"applied": applied, "failed": skipped}


def reject_groups(path: Path, group_ids: list[int]) -> None:
    record = json.loads(path.read_text(encoding="utf-8"))
    for g in record["groups"]:
        if g["id"] in group_ids and g["status"] == "pending":
            g["status"] = "rejected"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
