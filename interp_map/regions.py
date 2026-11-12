"""Structure for big graphs: regions of the field, and how important each document is.

Regions are a small set of concepts that each cover many documents but overlap little with
one another; they're chosen greedily from the concept DAG. They give the lineage graph its
horizontal bands and the concept graph its zoomed-out "map" view. The analysis is computed
on the fly, so it adapts as the map grows.

Importance measures how much later work builds on a document: PageRank over "later document
builds on earlier document" edges, where an edge comes from an explicit citation, an inferred
influence, or a later document engaging with a claim the earlier one proposed.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .report import concept_descendants


@dataclass
class Region:
    concept_id: int
    slug: str
    name: str
    documents: set[int]
    exclusive: int = 0                     # documents assigned to this region
    concepts: set[int] = field(default_factory=set)


def _concept_docs(conn) -> tuple[dict[int, set[int]], dict[int, set[int]], dict[int, dict[int, str]]]:
    """Per concept (counting sub-concepts): all documents tagged with it, and the documents for
    which it is a primary concept. Also each document's direct concept roles."""
    direct, primary = defaultdict(set), defaultdict(set)
    roles = defaultdict(dict)
    for doc_id, cid, role in conn.execute(
            "SELECT dc.document_id, dc.concept_id, dc.role FROM document_concepts dc "
            "JOIN documents d ON d.id = dc.document_id WHERE d.status != 'stub'"):
        direct[cid].add(doc_id)
        if role == "primary":
            primary[cid].add(doc_id)
        roles[doc_id][cid] = role
    desc = concept_descendants(conn)
    roll_up = lambda m: {cid: set().union(*(m[d] for d in ds)) for cid, ds in desc.items()}  # noqa: E731
    return roll_up(direct), roll_up(primary), roles


def choose_regions(conn, max_regions: int = 10, min_share: float = 0.03, max_share: float = 0.6,
                   overlap_penalty: float = 0.5) -> dict:
    """Greedy set cover with an overlap penalty.

    Coverage counts the documents whose *primary* concepts fall in a region (where a document
    lives), not passing mentions. A candidate's gain is the number of such documents it would
    newly cover, minus a penalty for each one already covered by a chosen region. Concepts that
    cover almost everything (umbrella topics) or almost nothing are not candidates, and nothing
    that is an ancestor or descendant of a chosen region can be chosen (it would overlap by
    construction).
    """
    docs_of, home_of, roles = _concept_docs(conn)
    names = dict(conn.execute("SELECT id, name FROM concepts"))
    slugs = dict(conn.execute("SELECT id, slug FROM concepts"))
    all_docs = {r[0] for r in conn.execute("SELECT id FROM documents WHERE status != 'stub'")}
    n = len(all_docs) or 1
    desc = concept_descendants(conn)
    anc = defaultdict(set)
    for c, ds in desc.items():
        for d in ds:
            anc[d].add(c)

    lo, hi = max(2, min_share * n), max(2, max_share * n)
    candidates = {c: home_of[c] for c, ds in docs_of.items() if lo <= len(ds) <= hi and home_of[c]}
    chosen: list[int] = []
    covered: set[int] = set()
    while len(chosen) < max_regions and candidates:
        def gain(c):
            return len(candidates[c] - covered) - overlap_penalty * len(candidates[c] & covered)
        best = max(candidates, key=lambda c: (gain(c), -len(candidates[c])))
        if gain(best) < max(1, 0.02 * n):
            break
        chosen.append(best)
        covered |= candidates[best]
        blocked = desc[best] | anc[best]
        candidates = {c: ds for c, ds in candidates.items() if c not in blocked}

    regions = [Region(c, slugs[c], names[c], docs_of[c], concepts=desc[c]) for c in chosen]
    by_slug = {r.slug: r for r in regions}
    doc_region = assign_documents(regions, roles)
    for r in doc_region.values():
        by_slug[r].exclusive += 1
    concept_region = assign_concepts(conn, regions, chosen)
    minor = minor_regions(conn, concept_region, docs_of, desc, names, slugs)
    for m in minor:
        for c in m.concepts:
            concept_region.setdefault(c, m.slug)
    overlap = [[round(len(a.documents & b.documents) / (len(a.documents | b.documents) or 1), 2)
                for b in regions] for a in regions]
    return {
        "regions": regions,
        "minor_regions": minor,                 # concept-map-only groups for leftover areas
        "document_region": doc_region,          # doc id -> region slug (unassigned docs absent)
        "concept_region": concept_region,       # concept id -> region slug (main or minor)
        "overlap": overlap,                     # Jaccard between regions' document sets
        "coverage": len(covered) / n,
        "documents": n,
    }


def minor_regions(conn, concept_region: dict[int, str], docs_of, desc, names, slugs) -> list[Region]:
    """Group concepts no main region claimed under their top-level area (the concept just
    below a root), so the concept map has no loose ends. Groups of one are left alone."""
    parents = defaultdict(set)
    for child, parent in conn.execute("SELECT child_id, parent_id FROM concept_relations WHERE relation = 'subtopic_of'"):
        parents[child].add(parent)
    roots = {c for c in names if not parents[c]}

    def top_area(c, seen=()):
        if c in roots:
            return None
        if parents[c] & roots:
            return c
        ups = [top_area(p, seen + (c,)) for p in parents[c] if p not in seen]
        return next((u for u in ups if u is not None), None)

    groups = defaultdict(set)
    for c in names:
        if c not in concept_region and c not in roots:
            area = top_area(c)
            if area is not None and area not in concept_region:
                groups[area].add(c)
    return [Region(a, slugs[a], names[a], docs_of.get(a, set()), concepts=cs | {a})
            for a, cs in sorted(groups.items(), key=lambda kv: -len(kv[1])) if len(cs | {a}) >= 2]


def assign_documents(regions: list[Region], roles: dict[int, dict[int, str]]) -> dict[int, str]:
    """Each document's home region: where its concepts sit, primary concepts counting double.
    Ties go to the smaller (more specific) region."""
    out = {}
    for doc_id, cs in roles.items():
        scores = {}
        for r in regions:
            s = sum(2 if role == "primary" else 1 for c, role in cs.items() if c in r.concepts)
            if s:
                scores[r.slug] = (s, -len(r.documents))
        if scores:
            out[doc_id] = max(scores, key=scores.get)
    return out


def assign_concepts(conn, regions: list[Region], chosen: list[int]) -> dict[int, str]:
    """Each concept's region: the nearest region concept above it (or itself)."""
    parents = defaultdict(set)
    for child, parent in conn.execute("SELECT child_id, parent_id FROM concept_relations WHERE relation = 'subtopic_of'"):
        parents[child].add(parent)
    region_of = {c: r.slug for c, r in zip(chosen, regions)}
    size = {r.slug: len(r.documents) for r in regions}
    out = {}
    for (cid,) in conn.execute("SELECT id FROM concepts"):
        # breadth-first upward search; the first level with a region wins
        frontier, seen = {cid}, {cid}
        while frontier:
            hits = [region_of[c] for c in frontier if c in region_of]
            if hits:
                out[cid] = min(hits, key=lambda s: size[s])
                break
            frontier = {p for c in frontier for p in parents[c]} - seen
            seen |= frontier
    return out


def importance(conn, damping: float = 0.85, iterations: int = 50) -> dict[int, dict]:
    """PageRank over 'later document builds on earlier document' edges among ingested documents."""
    dates = {r[0]: r[1] or "" for r in conn.execute("SELECT id, published_date FROM documents WHERE status != 'stub'")}
    edges = defaultdict(set)   # later -> earlier
    cited_by = defaultdict(set)
    for citing, cited in conn.execute("SELECT citing_id, cited_id FROM document_links"):
        if citing in dates and cited in dates and citing != cited and not (dates[cited] > dates[citing] != ""):
            edges[citing].add(cited)
            cited_by[cited].add(citing)
    # idea uptake: later documents that engage with a claim this document proposed
    proposers = defaultdict(set)
    for claim_id, doc_id in conn.execute("SELECT claim_id, document_id FROM claim_evidence WHERE relation = 'proposes'"):
        if doc_id in dates:
            proposers[claim_id].add(doc_id)
    uptake = defaultdict(set)
    for claim_id, doc_id in conn.execute("SELECT claim_id, document_id FROM claim_evidence WHERE relation != 'proposes'"):
        for p in proposers.get(claim_id, ()):
            if doc_id in dates and doc_id != p and dates[doc_id] >= dates[p]:
                edges[doc_id].add(p)
                uptake[p].add(doc_id)

    nodes = list(dates)
    n = len(nodes) or 1
    rank = {d: 1 / n for d in nodes}
    for _ in range(iterations):
        nxt = {d: (1 - damping) / n for d in nodes}
        dangling = sum(rank[d] for d in nodes if not edges[d])
        for d in nodes:
            out = edges[d]
            for e in out:
                nxt[e] += damping * rank[d] / len(out)
        for d in nodes:
            nxt[d] += damping * dangling / n
        rank = nxt
    # percentile = share of documents scoring strictly lower (ties share a percentile)
    scores = sorted(rank.values())
    from bisect import bisect_left
    pct = {d: bisect_left(scores, rank[d] - 1e-12) / max(1, n - 1) for d in nodes}
    return {d: {"score": rank[d], "percentile": pct[d], "cited_by": len(cited_by[d]),
                "idea_uptake": len(uptake[d])} for d in nodes}
