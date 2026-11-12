"""Human edits made while reviewing: editing, merging and deleting claims and concepts,
and correcting how a document bears on claims.

Every operation here validates its input and records itself in `edit_log`. Edits that are
specific to one document (its evidence links and concepts) mark that document `curated`, so
re-importing its extraction can't silently undo them (see `db.ingest_extraction`). Claim and
concept edits need no such lock: imports never overwrite an existing statement or name, and
merges and deletions leave a `vocab_aliases` entry so the old slug resolves correctly later.
"""
from __future__ import annotations

import json
from typing import Optional

from . import db


class CurationError(ValueError):
    """An edit that would leave the map inconsistent (unknown slug, cycle, clash)."""


TABLE = {"claim": "claims", "concept": "concepts", "document": "documents"}
RELATIONS = ("proposes", "assumes", "supports", "refutes", "mixed",
             "replicates", "fails_to_replicate", "extends", "critiques")


def _id(conn, kind: str, slug: str) -> int:
    row = conn.execute(f"SELECT id FROM {TABLE[kind]} WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        raise CurationError(f"no {kind} {slug!r}")
    return row[0]


def log(conn, action: str, target_type: str, target_slug: str, detail: Optional[dict] = None,
        user: str = "me") -> None:
    user_id = conn.execute("SELECT id FROM users WHERE handle = ?", (user,)).fetchone()[0]
    conn.execute("INSERT INTO edit_log (user_id, action, target_type, target_slug, detail) VALUES (?, ?, ?, ?, ?)",
                 (user_id, action, target_type, target_slug, json.dumps(detail) if detail else None))


def _mark_curated(conn, doc_ids) -> None:
    conn.executemany("UPDATE documents SET curated = 1, updated_at = datetime('now') WHERE id = ?",
                     [(d,) for d in set(doc_ids)])


def _docs_touching(conn, kind: str, obj_id: int) -> list[int]:
    sql = {"claim": "SELECT document_id FROM claim_evidence WHERE claim_id = ?",
           "concept": "SELECT document_id FROM document_concepts WHERE concept_id = ?"}[kind]
    return [r[0] for r in conn.execute(sql, (obj_id,))]


def _ancestors(conn, edges_sql: str, start: int) -> set[int]:
    """Everything reachable upward from `start` (parents of parents, or dependencies of dependencies)."""
    seen, stack = set(), [start]
    while stack:
        for (up,) in conn.execute(edges_sql, (stack.pop(),)):
            if up not in seen:
                seen.add(up)
                stack.append(up)
    return seen


# ---------------------------------------------------------------- claims

def update_claim(conn, slug: str, *, statement: str, claim_type: str, scope: Optional[str],
                 concepts: list[str], depends_on: list[str]) -> None:
    cid = _id(conn, "claim", slug)
    if not statement.strip():
        raise CurationError("statement can't be empty")
    concept_ids = [_id(conn, "concept", c) for c in dict.fromkeys(concepts)]
    dep_ids = [_id(conn, "claim", d) for d in dict.fromkeys(depends_on)]
    for d, dslug in zip(dep_ids, dict.fromkeys(depends_on)):
        # X may depend on Y only if Y doesn't (transitively) depend on X
        if d == cid or cid in _ancestors(conn, "SELECT depends_on_id FROM claim_dependencies WHERE claim_id = ?", d):
            raise CurationError(f"{slug} can't depend on {dslug}: that would make a dependency loop")
    before = dict(conn.execute("SELECT statement, claim_type, scope FROM claims WHERE id = ?", (cid,)).fetchone())
    conn.execute("UPDATE claims SET statement = ?, claim_type = ?, scope = ? WHERE id = ?",
                 (statement.strip(), claim_type, (scope or "").strip() or None, cid))
    conn.execute("DELETE FROM claim_concepts WHERE claim_id = ?", (cid,))
    conn.executemany("INSERT INTO claim_concepts VALUES (?, ?)", [(cid, k) for k in concept_ids])
    conn.execute("DELETE FROM claim_dependencies WHERE claim_id = ?", (cid,))
    conn.executemany("INSERT INTO claim_dependencies (claim_id, depends_on_id) VALUES (?, ?)",
                     [(cid, d) for d in dep_ids])
    log(conn, "edit", "claim", slug, {"before": before, "concepts": concepts, "depends_on": depends_on})
    conn.commit()


def create_claim(conn, *, statement: str, claim_type: str = "empirical", concepts: list[str] = ()) -> str:
    if not statement.strip():
        raise CurationError("statement can't be empty")
    base = db.slugify(" ".join(statement.split()[:8]))
    slug = db._unique_slug(conn, "claims", base)
    cid = db.upsert_claim(conn, slug, statement.strip(), claim_type)
    for c in concepts:
        conn.execute("INSERT OR IGNORE INTO claim_concepts VALUES (?, ?)", (cid, _id(conn, "concept", c)))
    log(conn, "create", "claim", slug)
    conn.commit()
    return slug


# ---------------------------------------------------------------- concepts

def update_concept(conn, slug: str, *, name: str, kind: str, description: Optional[str],
                   parents: list[str]) -> None:
    cid = _id(conn, "concept", slug)
    if not name.strip():
        raise CurationError("name can't be empty")
    parent_ids = [_id(conn, "concept", p) for p in dict.fromkeys(parents)]
    for p, pslug in zip(parent_ids, dict.fromkeys(parents)):
        # a parent can't also be a descendant
        if p == cid or cid in _ancestors(conn, "SELECT parent_id FROM concept_relations WHERE relation = 'subtopic_of' AND child_id = ?", p):
            raise CurationError(f"{pslug} is already under {slug}; making it a parent would create a loop")
    before = dict(conn.execute("SELECT name, kind, description FROM concepts WHERE id = ?", (cid,)).fetchone())
    conn.execute("UPDATE concepts SET name = ?, kind = ?, description = ? WHERE id = ?",
                 (name.strip(), kind, (description or "").strip() or None, cid))
    conn.execute("DELETE FROM concept_relations WHERE relation = 'subtopic_of' AND child_id = ?", (cid,))
    conn.executemany("INSERT INTO concept_relations (child_id, parent_id) VALUES (?, ?)", [(cid, p) for p in parent_ids])
    log(conn, "edit", "concept", slug, {"before": before, "parents": parents})
    conn.commit()


def create_concept(conn, *, name: str, kind: str = "topic", description: Optional[str] = None,
                   parents: list[str] = ()) -> str:
    if not name.strip():
        raise CurationError("name can't be empty")
    slug = db._unique_slug(conn, "concepts", db.slugify(name))
    cid = db.upsert_concept(conn, slug, name.strip(), (description or "").strip() or None, kind)
    for p in parents:
        conn.execute("INSERT INTO concept_relations (child_id, parent_id) VALUES (?, ?)", (cid, _id(conn, "concept", p)))
    log(conn, "create", "concept", slug)
    conn.commit()
    return slug


# ---------------------------------------------------------------- merge & delete (claims and concepts)

def merge(conn, kind: str, from_slug: str, into_slug: str) -> dict:
    if from_slug == into_slug:
        raise CurationError("can't merge something into itself")
    src, dst = _id(conn, kind, from_slug), _id(conn, kind, into_slug)
    moved = len(_docs_touching(conn, kind, src))
    db.merge(conn, kind, from_slug, into_slug)  # re-points edges, records the alias, commits
    log(conn, "merge", kind, into_slug, {"from": from_slug, "documents_moved": moved})
    conn.commit()
    return {"into": into_slug, "documents_moved": moved}


def delete(conn, kind: str, slug: str) -> None:
    oid = _id(conn, kind, slug)
    conn.execute(f"DELETE FROM {TABLE[kind]} WHERE id = ?", (oid,))  # edges cascade
    conn.execute("DELETE FROM annotations WHERE target_type = ? AND target_id = ?", (kind, oid))
    # keep it deleted: future extractions using this slug skip it
    conn.execute("INSERT OR REPLACE INTO vocab_aliases VALUES (?, ?, NULL)", (kind, slug))
    log(conn, "delete", kind, slug)
    conn.commit()


def dismiss_duplicate(conn, kind: str, a: str, b: str) -> None:
    a, b = sorted((a, b))
    conn.execute("INSERT OR IGNORE INTO vocab_dismissed VALUES (?, ?, ?)", (kind, a, b))
    log(conn, "not_duplicates", kind, a, {"other": b})
    conn.commit()


def similar(conn, kind: str, slug: str, n: int = 5) -> list[tuple[float, str]]:
    """Closest other labels by word overlap: candidates to merge with."""
    sql = {"claim": "SELECT slug, statement AS text FROM claims",
           "concept": "SELECT slug, name || ' ' || COALESCE(description, '') AS text FROM concepts"}[kind]
    rows = {r["slug"]: db._tokens(r["text"]) for r in conn.execute(sql)}
    if slug not in rows:
        raise CurationError(f"no {kind} {slug!r}")
    dismissed = {tuple(r) for r in conn.execute("SELECT kind, a, b FROM vocab_dismissed")}
    mine = rows.pop(slug)
    scored = [(len(mine & t) / len(mine | t), s) for s, t in rows.items() if mine and t
              and (kind, *sorted((slug, s))) not in dismissed]
    return [x for x in sorted(scored, reverse=True)[:n] if x[0] >= 0.2]


# ---------------------------------------------------------------- evidence (how a document bears on a claim)

def _check_evidence(relation: str, strength: Optional[str]) -> None:
    if relation not in RELATIONS:
        raise CurationError(f"unknown relation {relation!r}")
    if strength not in (None, "weak", "moderate", "strong"):
        raise CurationError(f"unknown strength {strength!r}")


def update_evidence(conn, evidence_id: int, *, relation: str, strength: Optional[str],
                    experiment_summary: Optional[str]) -> None:
    _check_evidence(relation, strength)
    row = conn.execute("SELECT e.*, c.slug AS claim_slug FROM claim_evidence e JOIN claims c ON c.id = e.claim_id "
                       "WHERE e.id = ?", (evidence_id,)).fetchone()
    if row is None:
        raise CurationError("no such evidence link")
    clash = conn.execute("SELECT 1 FROM claim_evidence WHERE claim_id = ? AND document_id = ? AND relation = ? AND id != ?",
                         (row["claim_id"], row["document_id"], relation, evidence_id)).fetchone()
    if clash:
        raise CurationError(f"this document already {relation} that claim")
    conn.execute("UPDATE claim_evidence SET relation = ?, strength = ?, experiment_summary = ?, reviewed = 1, "
                 "provenance = COALESCE(provenance, '') || ' +edited' WHERE id = ?",
                 (relation, strength, (experiment_summary or "").strip() or None, evidence_id))
    _mark_curated(conn, [row["document_id"]])
    log(conn, "edit_evidence", "claim", row["claim_slug"],
        {"document_id": row["document_id"], "before": [row["relation"], row["strength"]], "after": [relation, strength]})
    conn.commit()


def delete_evidence(conn, evidence_id: int) -> None:
    row = conn.execute("SELECT e.*, c.slug AS claim_slug FROM claim_evidence e JOIN claims c ON c.id = e.claim_id "
                       "WHERE e.id = ?", (evidence_id,)).fetchone()
    if row is None:
        raise CurationError("no such evidence link")
    conn.execute("DELETE FROM claim_evidence WHERE id = ?", (evidence_id,))
    _mark_curated(conn, [row["document_id"]])
    log(conn, "delete_evidence", "claim", row["claim_slug"], {"document_id": row["document_id"], "relation": row["relation"]})
    conn.commit()


def add_evidence(conn, document_slug: str, claim_slug: str, *, relation: str, strength: Optional[str] = None,
                 experiment_summary: Optional[str] = None) -> None:
    _check_evidence(relation, strength)
    doc_id, claim_id = _id(conn, "document", document_slug), _id(conn, "claim", claim_slug)
    if conn.execute("SELECT 1 FROM claim_evidence WHERE claim_id = ? AND document_id = ? AND relation = ?",
                    (claim_id, doc_id, relation)).fetchone():
        raise CurationError(f"this document already {relation} that claim")
    conn.execute("""INSERT INTO claim_evidence (claim_id, document_id, relation, strength, experiment_summary,
                    provenance, reviewed) VALUES (?, ?, ?, ?, ?, 'manual', 1)""",
                 (claim_id, doc_id, relation, strength, (experiment_summary or "").strip() or None))
    _mark_curated(conn, [doc_id])
    log(conn, "add_evidence", "claim", claim_slug, {"document": document_slug, "relation": relation})
    conn.commit()


def set_document_concepts(conn, document_slug: str, concepts: list[tuple[str, str]]) -> None:
    doc_id = _id(conn, "document", document_slug)
    rows = [(doc_id, _id(conn, "concept", slug), role if role in ("primary", "secondary") else "secondary")
            for slug, role in dict(concepts).items()]
    conn.execute("DELETE FROM document_concepts WHERE document_id = ?", (doc_id,))
    conn.executemany("INSERT INTO document_concepts VALUES (?, ?, ?)", rows)
    _mark_curated(conn, [doc_id])
    log(conn, "edit_concepts", "document", document_slug, {"concepts": concepts})
    conn.commit()
