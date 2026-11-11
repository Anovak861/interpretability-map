"""Local web interface: a JSON API over the database plus a single-page frontend.

    .venv/Scripts/python -m interp_map.web.server        (http://127.0.0.1:8765)

Read endpoints compute field health on the fly via `report.py`, exactly as the CLI does.
Write endpoints cover notes, review (marking extractions reviewed, overriding a claim's
status) and curation: editing, merging and deleting claims and concepts, and correcting
how a document bears on claims. Curation goes through `curation.py`, which validates
edits, logs them, and protects them from being undone by later imports.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import curation, db, regions, report

STATIC = Path(__file__).with_name("static")
app = FastAPI(title="Interpretability Map")


@app.middleware("http")
async def no_stale_frontend(request, call_next):
    # the frontend changes as the project grows; always revalidate instead of serving stale JS/CSS
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.exception_handler(curation.CurationError)
async def curation_error(request: Request, exc: curation.CurationError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


def get_conn():
    # one connection per request, but the framework may open it on a different worker thread
    # than the one that runs the endpoint
    conn = db.connect(check_same_thread=False)
    try:
        yield conn
    finally:
        conn.close()


def _row(conn, table: str, slug: str):
    row = conn.execute(f"SELECT * FROM {table} WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        raise HTTPException(404, f"no {table[:-1]} {slug!r}")
    return row


def _authors(conn) -> dict[int, list[str]]:
    out = defaultdict(list)
    for r in conn.execute("SELECT da.document_id, a.name FROM document_authors da "
                          "JOIN authors a ON a.id = da.author_id ORDER BY da.document_id, da.position"):
        out[r[0]].append(r[1])
    return out


def _notes(conn, target_type: str, target_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT a.id, a.kind, a.body, a.visibility, a.created_at, u.handle AS author FROM annotations a "
        "JOIN users u ON u.id = a.user_id WHERE target_type = ? AND target_id = ? ORDER BY a.created_at DESC",
        (target_type, target_id))]


def _health_dict(h: report.ClaimHealth) -> dict:
    return {"slug": h.slug, "statement": h.statement, "status": h.status, "derived_status": h.derived_status,
            "positive": h.positive_docs, "negative": h.negative_docs, "mixed": h.mixed_docs,
            "groups": h.independent_groups, "assumed_by": h.assumed_by, "critiqued_by": h.critiqued_by,
            "dependents": h.depended_on_by, "load": h.load, "fragile": h.fragile_foundation,
            "concepts": h.concepts, "level": h.level, "findings": h.findings}


def _doc_summary(r, authors) -> dict:
    return {"slug": r["slug"], "title": r["title"], "doc_type": r["doc_type"], "source": r["source"],
            "url": r["url"], "published_date": r["published_date"], "status": r["status"],
            "authors": authors.get(r["id"], []), "provenance": r["provenance"]}


# ---------------------------------------------------------------- overview

@app.get("/api/overview")
def overview(conn=Depends(get_conn)):
    claims = report.claim_health(conn)
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("concepts", "claims", "annotations")}
    docs_by_status = dict(conn.execute("SELECT status, COUNT(*) FROM documents GROUP BY status").fetchall())
    status_mix = defaultdict(int)
    for h in claims.values():
        status_mix[h.status] += 1
    fragile = sorted((h for h in claims.values() if h.fragile_foundation), key=lambda h: -h.load)
    authors = _authors(conn)
    recent = [_doc_summary(r, authors) for r in conn.execute(
        "SELECT * FROM documents WHERE status != 'stub' ORDER BY updated_at DESC LIMIT 8")]
    notes = [dict(r) for r in conn.execute(
        """SELECT a.id, a.kind, a.body, a.created_at, a.target_type,
                  COALESCE(d.slug, c.slug, k.slug) AS target_slug,
                  COALESCE(d.title, c.statement, k.name) AS target_label
           FROM annotations a
           LEFT JOIN documents d ON a.target_type = 'document' AND d.id = a.target_id
           LEFT JOIN claims c ON a.target_type = 'claim' AND c.id = a.target_id
           LEFT JOIN concepts k ON a.target_type = 'concept' AND k.id = a.target_id
           ORDER BY a.created_at DESC LIMIT 6""")]
    last_run = conn.execute("SELECT started_at, summary FROM update_runs ORDER BY id DESC LIMIT 1").fetchone()
    return {
        "counts": {**counts, "documents": sum(v for k, v in docs_by_status.items() if k != "stub"),
                   "stubs": docs_by_status.get("stub", 0), "unreviewed": docs_by_status.get("extracted", 0)},
        "status_mix": status_mix,
        "concepts": [c for c in report.concept_health(conn, claims) if c["claims"]],
        "fragile": [_health_dict(h) for h in fragile[:12]],
        "recent_documents": recent,
        "recent_notes": notes,
        "last_run": {"started_at": last_run["started_at"], **json.loads(last_run["summary"])} if last_run else None,
    }


# ---------------------------------------------------------------- documents

@app.get("/api/documents")
def list_documents(include_stubs: bool = False, conn=Depends(get_conn)):
    authors = _authors(conn)
    claim_counts = dict(conn.execute("SELECT document_id, COUNT(*) FROM claim_evidence GROUP BY document_id"))
    cited_by = dict(conn.execute("SELECT cited_id, COUNT(*) FROM document_links GROUP BY cited_id"))
    concepts = defaultdict(list)
    for r in conn.execute("SELECT dc.document_id, c.slug FROM document_concepts dc "
                          "JOIN concepts c ON c.id = dc.concept_id WHERE dc.role = 'primary'"):
        concepts[r[0]].append(r[1])
    where = "" if include_stubs else "WHERE status != 'stub'"
    return [{**_doc_summary(r, authors), "claims": claim_counts.get(r["id"], 0),
             "cited_by": cited_by.get(r["id"], 0), "primary_concepts": concepts[r["id"]]}
            for r in conn.execute(f"SELECT * FROM documents {where} ORDER BY published_date DESC")]


@app.get("/api/documents/{slug}")
def get_document(slug: str, conn=Depends(get_conn)):
    d = _row(conn, "documents", slug)
    authors = _authors(conn)
    health = {h.id: h for h in report.claim_health(conn).values()}
    claims = [{**_health_dict(health[r["claim_id"]]), "evidence_id": r["id"], "relation": r["relation"],
               "strength": r["strength"],
               "experiment_summary": r["experiment_summary"], "location": r["location"],
               "models_studied": json.loads(r["models_studied"]) if r["models_studied"] else []}
              for r in conn.execute("SELECT * FROM claim_evidence WHERE document_id = ?", (d["id"],))]

    def links(sql):
        out = []
        for r in conn.execute(sql, (d["id"],)):
            later = (r["citing_date"] and r["cited_date"] and r["cited_date"] > r["citing_date"])
            out.append({**_doc_summary(r, authors), "link_type": r["link_type"], "confidence": r["confidence"],
                        "rationale": r["rationale"], "later_link": bool(later)})
        return out

    base = """SELECT o.*, l.link_type, l.confidence, l.rationale, citing.published_date AS citing_date,
                     cited.published_date AS cited_date FROM document_links l
              JOIN documents citing ON citing.id = l.citing_id JOIN documents cited ON cited.id = l.cited_id"""
    return {
        **_doc_summary(d, authors), "abstract": d["abstract"], "summary": d["summary"],
        "provenance": d["provenance"], "external_ids": json.loads(d["external_ids"] or "{}"),
        "curated": bool(d["curated"]),
        "concepts": [dict(r) for r in conn.execute(
            "SELECT c.slug, c.name, c.kind, dc.role FROM document_concepts dc JOIN concepts c ON c.id = dc.concept_id "
            "WHERE dc.document_id = ? ORDER BY dc.role, c.name", (d["id"],))],
        "claims": claims,
        "builds_on": links(base + " JOIN documents o ON o.id = l.cited_id WHERE l.citing_id = ? ORDER BY o.published_date"),
        "built_on_by": links(base + " JOIN documents o ON o.id = l.citing_id WHERE l.cited_id = ? ORDER BY o.published_date"),
        "notes": _notes(conn, "document", d["id"]),
    }


class ReviewIn(BaseModel):
    reviewed: bool


@app.post("/api/documents/{slug}/review")
def review_document(slug: str, body: ReviewIn, conn=Depends(get_conn)):
    d = _row(conn, "documents", slug)
    if d["status"] not in ("extracted", "reviewed"):
        raise HTTPException(400, "only extracted documents can be reviewed")
    status = "reviewed" if body.reviewed else "extracted"
    conn.execute("UPDATE documents SET status = ?, updated_at = datetime('now') WHERE id = ?", (status, d["id"]))
    conn.execute("UPDATE claim_evidence SET reviewed = ? WHERE document_id = ?", (int(body.reviewed), d["id"]))
    conn.commit()
    return {"status": status}


# ---------------------------------------------------------------- claims

@app.get("/api/claims")
def list_claims(conn=Depends(get_conn)):
    types = dict(conn.execute("SELECT id, claim_type FROM claims"))
    return [{**_health_dict(h), "claim_type": types[h.id]} for h in report.claim_health(conn).values()]


@app.get("/api/claims/{slug}")
def get_claim(slug: str, conn=Depends(get_conn)):
    c = _row(conn, "claims", slug)
    health = report.claim_health(conn)
    authors = _authors(conn)
    evidence = [{**_doc_summary(r, authors), "evidence_id": r["evidence_id"], "relation": r["relation"],
                 "strength": r["strength"],
                 "experiment_summary": r["experiment_summary"], "location": r["location"],
                 "reviewed": bool(r["reviewed"]),
                 "models_studied": json.loads(r["models_studied"]) if r["models_studied"] else []}
                for r in conn.execute(
                    "SELECT d.*, e.id AS evidence_id, e.relation, e.strength, e.experiment_summary, e.location, "
                    "e.models_studied, "
                    "e.reviewed FROM claim_evidence e JOIN documents d ON d.id = e.document_id "
                    "WHERE e.claim_id = ? ORDER BY d.published_date", (c["id"],))]
    by_id = {h.id: h for h in health.values()}
    return {
        **_health_dict(by_id[c["id"]]), "claim_type": c["claim_type"], "scope": c["scope"],
        "status_override": c["status_override"], "status_note": c["status_note"],
        "evidence": evidence,
        "depends_on_claims": [_health_dict(by_id[r[0]]) for r in conn.execute(
            "SELECT depends_on_id FROM claim_dependencies WHERE claim_id = ?", (c["id"],))],
        "dependent_claims": [_health_dict(by_id[r[0]]) for r in conn.execute(
            "SELECT claim_id FROM claim_dependencies WHERE depends_on_id = ?", (c["id"],))],
        "concept_details": [dict(r) for r in conn.execute(
            "SELECT k.slug, k.name FROM claim_concepts cc JOIN concepts k ON k.id = cc.concept_id WHERE cc.claim_id = ?",
            (c["id"],))],
        "notes": _notes(conn, "claim", c["id"]),
        # the hierarchy: what this claim is filed under, and what is filed under it
        "filed_under": [{**_health_dict(by_id[r[0]]), "stance": r[1]} for r in conn.execute(
            "SELECT parent_id, stance FROM claim_links WHERE finding_id = ?", (c["id"],))],
        "filed_findings": [{**_health_dict(by_id[r[0]]), "stance": r[1]} for r in conn.execute(
            "SELECT finding_id, stance FROM claim_links WHERE parent_id = ?", (c["id"],))],
    }


@app.get("/api/foundations")
def foundations(conn=Depends(get_conn)):
    """Paradigms and broad claims, with what each rests on and how its findings split by stance."""
    health = {h.id: h for h in report.claim_health(conn).values()}
    top = {i: h for i, h in health.items() if h.level in ("paradigm", "thesis")}
    stances = {i: {} for i in top}
    for parent, stance, n in conn.execute("SELECT parent_id, stance, COUNT(*) FROM claim_links GROUP BY 1, 2"):
        if parent in stances:
            stances[parent][stance] = n
    rests, needs = {i: [] for i in top}, {i: [] for i in top}
    for a, b in conn.execute("SELECT claim_id, depends_on_id FROM claim_dependencies"):
        if a in top and b in top:
            (rests if top[b].level == "paradigm" else needs)[a].append(top[b].slug)
    names = dict(conn.execute("SELECT slug, name FROM concepts"))
    loose = conn.execute("SELECT COUNT(*) FROM claims c WHERE level = 'finding' AND NOT EXISTS "
                         "(SELECT 1 FROM claim_links l WHERE l.finding_id = c.id)").fetchone()[0]
    return {"claims": [{**_health_dict(h), "stances": stances[i], "rests_on": rests[i], "depends_on": needs[i]}
                       for i, h in top.items()],
            "concept_names": names, "unplaced_findings": loose,
            "findings": conn.execute("SELECT COUNT(*) FROM claims WHERE level = 'finding'").fetchone()[0]}


class StatusIn(BaseModel):
    status: Optional[Literal["untested", "single_source", "supported", "replicated", "contested", "refuted"]]
    note: Optional[str] = None


@app.post("/api/claims/{slug}/status")
def override_status(slug: str, body: StatusIn, conn=Depends(get_conn)):
    c = _row(conn, "claims", slug)
    conn.execute("UPDATE claims SET status_override = ?, status_note = ? WHERE id = ?",
                 (body.status, body.note if body.status else None, c["id"]))
    conn.commit()
    return {"ok": True}


# ---------------------------------------------------------------- concepts

@app.get("/api/concepts")
def list_concepts(conn=Depends(get_conn)):
    claims = report.claim_health(conn)
    health = {c["slug"]: c for c in report.concept_health(conn, claims)}
    rg = regions.choose_regions(conn)
    nodes = [{**dict(r), **health.get(r["slug"], {}), "region": rg["concept_region"].get(r["id"])}
             for r in conn.execute("SELECT id, slug, name, kind, description FROM concepts")]
    edges = [dict(r) for r in conn.execute(
        "SELECT c.slug AS child, p.slug AS parent, r.relation FROM concept_relations r "
        "JOIN concepts c ON c.id = r.child_id JOIN concepts p ON p.id = r.parent_id")]
    return {"nodes": nodes, "edges": edges, "regions": _regions_out(rg, health, include_minor=True)}


def _regions_out(rg: dict, health: dict, include_minor: bool = False) -> list[dict]:
    groups = [(r, False) for r in rg["regions"]] + ([(r, True) for r in rg["minor_regions"]] if include_minor else [])
    return [{"slug": r.slug, "name": r.name, "documents": len(r.documents), "home_documents": r.exclusive,
             "concepts": len(r.concepts), "minor": minor,
             **{k: health.get(r.slug, {}).get(k) for k in ("claims", "statuses", "solid_share", "fragile")}}
            for r, minor in groups]


@app.get("/api/concepts/{slug}")
def get_concept(slug: str, conn=Depends(get_conn)):
    k = _row(conn, "concepts", slug)
    claims = report.claim_health(conn)
    health = next((c for c in report.concept_health(conn, claims) if c["slug"] == slug), {})
    desc = report.concept_descendants(conn)[k["id"]]
    marks = ",".join("?" * len(desc))
    authors = _authors(conn)
    direct_claims = {r[0] for r in conn.execute("SELECT claim_id FROM claim_concepts WHERE concept_id = ?", (k["id"],))}
    all_claims = {r[0] for r in conn.execute(f"SELECT claim_id FROM claim_concepts WHERE concept_id IN ({marks})", tuple(desc))}
    docs = conn.execute(
        f"""SELECT d.*, MAX(dc.concept_id = ?) AS direct, MAX(dc.role = 'primary' AND dc.concept_id = ?) AS primary_here
            FROM document_concepts dc JOIN documents d ON d.id = dc.document_id
            WHERE dc.concept_id IN ({marks}) GROUP BY d.id ORDER BY d.published_date""",
        (k["id"], k["id"], *desc)).fetchall()
    return {
        "slug": k["slug"], "name": k["name"], "kind": k["kind"], "description": k["description"],
        "health": health,
        "parents": [dict(r) for r in conn.execute(
            "SELECT p.slug, p.name, r.relation FROM concept_relations r JOIN concepts p ON p.id = r.parent_id "
            "WHERE r.child_id = ?", (k["id"],))],
        "children": [dict(r) for r in conn.execute(
            "SELECT c.slug, c.name, r.relation FROM concept_relations r JOIN concepts c ON c.id = r.child_id "
            "WHERE r.parent_id = ?", (k["id"],))],
        "claims": [{**_health_dict(claims[cid]), "direct": cid in direct_claims} for cid in all_claims],
        "documents": [{**_doc_summary(r, authors), "direct": bool(r["direct"]), "primary": bool(r["primary_here"])}
                      for r in docs],
        "notes": _notes(conn, "concept", k["id"]),
    }


# ---------------------------------------------------------------- graphs

@app.get("/api/graph/lineage")
def lineage_graph(conn=Depends(get_conn)):
    """Citation lineage among ingested documents (stubs omitted; forward-in-time page-update links dropped)."""
    authors = _authors(conn)
    docs = {r["id"]: r for r in conn.execute("SELECT * FROM documents WHERE status != 'stub'")}
    edges = []
    for r in conn.execute("SELECT citing_id, cited_id, link_type, confidence FROM document_links"):
        a, b = docs.get(r["citing_id"]), docs.get(r["cited_id"])
        if not a or not b:
            continue
        if a["published_date"] and b["published_date"] and b["published_date"] > a["published_date"]:
            continue
        edges.append({"source": b["slug"], "target": a["slug"], "link_type": r["link_type"],
                      "confidence": r["confidence"]})
    primary = defaultdict(list)
    for r in conn.execute("SELECT dc.document_id, c.slug FROM document_concepts dc JOIN concepts c "
                          "ON c.id = dc.concept_id WHERE dc.role = 'primary'"):
        primary[r[0]].append(r[1])
    rg = regions.choose_regions(conn)
    imp = regions.importance(conn)
    health = {c["slug"]: c for c in report.concept_health(conn, report.claim_health(conn))}
    return {"nodes": [{**_doc_summary(d, authors), "primary_concepts": primary[i],
                       "region": rg["document_region"].get(i), **imp.get(i, {})} for i, d in docs.items()],
            "edges": edges, "regions": _regions_out(rg, health)}


@app.get("/api/documents/{slug}/lineage")
def document_lineage(slug: str, direction: Literal["back", "forward"] = "back", depth: int = 3,
                     conn=Depends(get_conn)):
    """Everything a document builds on (back) or everything built on it (forward), as a list.

    'Builds on' means a citation, or using a claim another document proposed. Back-tracing
    also reaches cited works not yet ingested, since those are part of the background too.
    """
    d = _row(conn, "documents", slug)
    depth = max(1, min(depth, 6))
    docs = {r["id"]: r for r in conn.execute("SELECT * FROM documents")}
    ingested = {i for i, r in docs.items() if r["status"] != "stub"}
    up = defaultdict(dict)    # doc -> {earlier doc: reason}
    for citing, cited, link_type in conn.execute("SELECT citing_id, cited_id, link_type FROM document_links"):
        a, b = docs.get(citing), docs.get(cited)
        if not a or not b or citing == cited:
            continue
        if a["published_date"] and b["published_date"] and b["published_date"] > a["published_date"]:
            continue  # page-update links that point forward in time
        up[citing].setdefault(cited, "inferred influence" if link_type == "inferred_influence" else "cited")
    proposer = defaultdict(set)
    for claim_id, doc_id in conn.execute("SELECT claim_id, document_id FROM claim_evidence WHERE relation = 'proposes'"):
        proposer[claim_id].add(doc_id)
    claim_text = dict(conn.execute("SELECT id, statement FROM claims"))
    for claim_id, doc_id, rel in conn.execute(
            "SELECT claim_id, document_id, relation FROM claim_evidence WHERE relation != 'proposes'"):
        for p in proposer.get(claim_id, ()):
            if p != doc_id and (docs[p]["published_date"] or "") <= (docs[doc_id]["published_date"] or "9999"):
                up[doc_id].setdefault(p, f"{rel} its claim: {claim_text[claim_id]}")
    down = defaultdict(dict)
    for later, earlier_map in up.items():
        for earlier, why in earlier_map.items():
            down[earlier][later] = why
    step = up if direction == "back" else down

    # breadth-first: shortest generation, and the document it was reached through
    found, frontier = {}, [d["id"]]
    for gen in range(1, depth + 1):
        nxt = []
        for cur in frontier:
            for other, why in step.get(cur, {}).items():
                if other == d["id"] or other in found:
                    continue
                found[other] = {"generation": gen, "via": cur, "why": why}
                if other in ingested:
                    nxt.append(other)
        frontier = nxt

    imp = regions.importance(conn)
    in_map_citations = defaultdict(int)
    for later in ingested:
        for earlier in up.get(later, {}):
            in_map_citations[earlier] += 1
    authors = _authors(conn)
    titles = {i: r["title"] for i, r in docs.items()}
    slugs = {i: r["slug"] for i, r in docs.items()}
    items = []
    for i, info in found.items():
        r = docs[i]
        items.append({**_doc_summary(r, authors), "generation": info["generation"],
                      "via": None if info["via"] == d["id"] else {"slug": slugs[info["via"]], "title": titles[info["via"]]},
                      "why": info["why"], "in_map": i in ingested,
                      "importance": imp.get(i, {}).get("percentile"),
                      "cited_in_map": in_map_citations[i]})
    return {"document": _doc_summary(d, authors), "direction": direction, "depth": depth, "items": items[:600],
            "truncated": len(items) > 600}


@app.post("/api/documents/{slug}/queue")
def queue_stub(slug: str, conn=Depends(get_conn)):
    """Queue a cited-but-not-ingested work for the next update run."""
    from .. import pipeline

    d = _row(conn, "documents", slug)
    if d["status"] != "stub":
        raise HTTPException(400, "already in the map")
    url = d["url"] or (f"https://arxiv.org/abs/{d['arxiv_id']}" if d["arxiv_id"] else None)
    if not url or "semanticscholar.org" in url:
        raise HTTPException(400, "no fetchable link for this work yet")
    pipeline.add_candidate(conn, url=url, title=d["title"], source="citation", reason="queued from a reading list")
    conn.execute("UPDATE candidates SET status = 'accepted', relevance = 3 WHERE url = ? AND status IN ('new', 'rejected')",
                 (db.normalize_url(url),))
    conn.commit()
    return {"ok": True}


# ---------------------------------------------------------------- notes

class NoteIn(BaseModel):
    target_type: Literal["document", "claim", "concept"]
    slug: str
    body: str
    kind: Literal["note", "idea", "critique", "question", "next_step", "dead_end"] = "note"


@app.get("/api/notes")
def list_notes(conn=Depends(get_conn)):
    return [dict(r) for r in conn.execute(
        """SELECT a.id, a.kind, a.body, a.visibility, a.created_at, a.target_type, u.handle AS author,
                  COALESCE(d.slug, c.slug, k.slug) AS target_slug,
                  COALESCE(d.title, c.statement, k.name) AS target_label
           FROM annotations a JOIN users u ON u.id = a.user_id
           LEFT JOIN documents d ON a.target_type = 'document' AND d.id = a.target_id
           LEFT JOIN claims c ON a.target_type = 'claim' AND c.id = a.target_id
           LEFT JOIN concepts k ON a.target_type = 'concept' AND k.id = a.target_id
           ORDER BY a.created_at DESC""")]


@app.post("/api/notes")
def create_note(body: NoteIn, conn=Depends(get_conn)):
    if not body.body.strip():
        raise HTTPException(400, "note is empty")
    try:
        nid = db.annotate(conn, body.target_type, body.slug, body.body.strip(), kind=body.kind)
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {"id": nid}


@app.delete("/api/notes/{note_id}")
def delete_note(note_id: int, conn=Depends(get_conn)):
    conn.execute("DELETE FROM annotations WHERE id = ?", (note_id,))
    conn.commit()
    return {"ok": True}


# ---------------------------------------------------------------- curation

ClaimType = Literal["empirical", "theoretical", "methodological", "hypothesis"]
ConceptKind = Literal["topic", "method", "phenomenon", "hypothesis", "tool", "model_component", "benchmark"]
Relation = Literal["proposes", "assumes", "supports", "refutes", "mixed", "replicates",
                   "fails_to_replicate", "extends", "critiques"]
Strength = Optional[Literal["weak", "moderate", "strong"]]


class ClaimEdit(BaseModel):
    statement: str
    claim_type: ClaimType
    scope: Optional[str] = None
    concepts: list[str] = []
    depends_on: list[str] = []


class ClaimNew(BaseModel):
    statement: str
    claim_type: ClaimType = "empirical"
    concepts: list[str] = []


class ConceptEdit(BaseModel):
    name: str
    kind: ConceptKind
    description: Optional[str] = None
    parents: list[str] = []


class MergeIn(BaseModel):
    into: str


class EvidenceEdit(BaseModel):
    relation: Relation
    strength: Strength = None
    experiment_summary: Optional[str] = None


class EvidenceNew(EvidenceEdit):
    claim: str


class DocConcept(BaseModel):
    slug: str
    role: Literal["primary", "secondary"] = "secondary"


class Dismiss(BaseModel):
    kind: Literal["claim", "concept"]
    a: str
    b: str


@app.put("/api/claims/{slug}")
def edit_claim(slug: str, body: ClaimEdit, conn=Depends(get_conn)):
    curation.update_claim(conn, slug, **body.model_dump())
    return {"ok": True}


@app.post("/api/claims")
def new_claim(body: ClaimNew, conn=Depends(get_conn)):
    return {"slug": curation.create_claim(conn, **body.model_dump())}


@app.put("/api/concepts/{slug}")
def edit_concept(slug: str, body: ConceptEdit, conn=Depends(get_conn)):
    curation.update_concept(conn, slug, **body.model_dump())
    return {"ok": True}


@app.post("/api/concepts")
def new_concept(body: ConceptEdit, conn=Depends(get_conn)):
    return {"slug": curation.create_concept(conn, **body.model_dump())}


@app.post("/api/{kind}s/{slug}/merge")
def merge_label(kind: Literal["claim", "concept"], slug: str, body: MergeIn, conn=Depends(get_conn)):
    return curation.merge(conn, kind, slug, body.into)


@app.delete("/api/{kind}s/{slug}")
def delete_label(kind: Literal["claim", "concept"], slug: str, conn=Depends(get_conn)):
    curation.delete(conn, kind, slug)
    return {"ok": True}


@app.get("/api/{kind}s/{slug}/similar")
def similar_labels(kind: Literal["claim", "concept"], slug: str, conn=Depends(get_conn)):
    label = "statement" if kind == "claim" else "name"
    out = []
    for score, other in curation.similar(conn, kind, slug):
        row = conn.execute(f"SELECT {label} AS label FROM {kind}s WHERE slug = ?", (other,)).fetchone()
        out.append({"slug": other, "label": row["label"], "score": round(score, 2)})
    return out


@app.put("/api/evidence/{evidence_id}")
def edit_evidence(evidence_id: int, body: EvidenceEdit, conn=Depends(get_conn)):
    curation.update_evidence(conn, evidence_id, **body.model_dump())
    return {"ok": True}


@app.delete("/api/evidence/{evidence_id}")
def remove_evidence(evidence_id: int, conn=Depends(get_conn)):
    curation.delete_evidence(conn, evidence_id)
    return {"ok": True}


@app.post("/api/documents/{slug}/evidence")
def new_evidence(slug: str, body: EvidenceNew, conn=Depends(get_conn)):
    data = body.model_dump()
    curation.add_evidence(conn, slug, data.pop("claim"), **data)
    return {"ok": True}


@app.put("/api/documents/{slug}/concepts")
def edit_document_concepts(slug: str, body: list[DocConcept], conn=Depends(get_conn)):
    curation.set_document_concepts(conn, slug, [(c.slug, c.role) for c in body])
    return {"ok": True}


@app.post("/api/duplicates/dismiss")
def dismiss(body: Dismiss, conn=Depends(get_conn)):
    curation.dismiss_duplicate(conn, body.kind, body.a, body.b)
    return {"ok": True}


class GroupIds(BaseModel):
    ids: list[int]


@app.get("/api/consolidation")
def consolidation_plan():
    from .. import consolidate

    path = consolidate.latest_plan()
    if not path:
        return {"plan": None}
    record = json.loads(path.read_text(encoding="utf-8"))
    return {"plan": path.name, "status": record.get("status"), "stopped": record.get("stopped"),
            "batches": record.get("batches"), "groups": record["groups"]}


@app.post("/api/consolidation/apply")
def consolidation_apply(body: GroupIds, conn=Depends(get_conn)):
    from .. import consolidate

    path = consolidate.latest_plan()
    if not path:
        raise HTTPException(404, "no consolidation plan")
    return consolidate.apply_groups(conn, path, body.ids)


@app.post("/api/consolidation/reject")
def consolidation_reject(body: GroupIds):
    from .. import consolidate

    path = consolidate.latest_plan()
    if not path:
        raise HTTPException(404, "no consolidation plan")
    consolidate.reject_groups(path, body.ids)
    return {"ok": True}


@app.get("/api/review")
def review_queue(conn=Depends(get_conn)):
    authors = _authors(conn)
    counts = dict(conn.execute("SELECT document_id, COUNT(*) FROM claim_evidence GROUP BY document_id"))
    queue = [{**_doc_summary(r, authors), "claims": counts.get(r["id"], 0), "curated": bool(r["curated"])}
             for r in conn.execute("SELECT * FROM documents WHERE status = 'extracted' ORDER BY created_at")]
    health = {h.slug: h for h in report.claim_health(conn).values()}
    concepts = {r["slug"]: dict(r) for r in conn.execute("SELECT slug, name, description, kind FROM concepts")}
    usage = {
        "claim": dict(conn.execute("SELECT c.slug, COUNT(e.id) FROM claims c LEFT JOIN claim_evidence e "
                                   "ON e.claim_id = c.id GROUP BY c.id")),
        "concept": dict(conn.execute("SELECT c.slug, COUNT(dc.document_id) FROM concepts c "
                                     "LEFT JOIN document_concepts dc ON dc.concept_id = c.id GROUP BY c.id")),
    }

    def side(kind, slug):
        if kind == "claim":
            h = health[slug]
            return {"slug": slug, "label": h.statement, "status": h.status, "uses": usage["claim"].get(slug, 0)}
        k = concepts[slug]
        return {"slug": slug, "label": k["name"], "detail": k["description"], "kind": k["kind"],
                "uses": usage["concept"].get(slug, 0)}

    all_dupes = db.possible_vocab_duplicates(conn)
    # strongest candidates first; resolving those usually removes many of the weaker pairs too
    dupes = [{"kind": kind, "score": round(score, 2), "a": side(kind, a), "b": side(kind, b)}
             for score, kind, a, b in all_dupes[:50]]
    edits = [dict(r) for r in conn.execute(
        "SELECT e.at, e.action, e.target_type, e.target_slug, e.detail, u.handle AS user FROM edit_log e "
        "LEFT JOIN users u ON u.id = e.user_id ORDER BY e.id DESC LIMIT 40")]
    return {"queue": queue, "duplicates": dupes, "duplicates_total": len(all_dupes), "edits": edits}


# ---------------------------------------------------------------- search

@app.get("/api/search")
def search(q: str, conn=Depends(get_conn)):
    like = f"%{q.strip()}%"
    return {
        "documents": [dict(r) for r in conn.execute(
            "SELECT slug, title AS label, source AS meta FROM documents WHERE status != 'stub' "
            "AND (title LIKE ? OR summary LIKE ?) LIMIT 8", (like, like))],
        "claims": [dict(r) for r in conn.execute(
            "SELECT slug, statement AS label, claim_type AS meta FROM claims WHERE statement LIKE ? OR slug LIKE ? LIMIT 8",
            (like, like))],
        "concepts": [dict(r) for r in conn.execute(
            "SELECT slug, name AS label, kind AS meta FROM concepts WHERE name LIKE ? OR slug LIKE ? "
            "OR description LIKE ? LIMIT 8", (like, like, like))],
        "authors": [dict(r) for r in conn.execute(
            "SELECT a.name AS label, COUNT(*) AS meta FROM authors a JOIN document_authors da ON da.author_id = a.id "
            "JOIN documents d ON d.id = da.document_id WHERE d.status != 'stub' AND a.name LIKE ? "
            "GROUP BY a.id LIMIT 6", (like,))],
    }


# ---------------------------------------------------------------- frontend

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


def main():
    import uvicorn

    conn = db.connect()
    db.init(conn)  # applies any new tables
    conn.close()
    import os
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("INTERP_MAP_PORT", 8765)))


if __name__ == "__main__":
    main()
