"""Derived field-health metrics. Nothing here is stored; it is all computed
from claim evidence, claim dependencies and the concept graph."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

POSITIVE = {"supports", "replicates"}
NEGATIVE = {"refutes", "fails_to_replicate"}
TESTED = POSITIVE | NEGATIVE | {"mixed"}
WEAK_STATUSES = {"untested", "single_source", "contested"}


@dataclass
class ClaimHealth:
    id: int
    slug: str
    statement: str
    status: str
    derived_status: str
    positive_docs: int = 0
    negative_docs: int = 0
    mixed_docs: int = 0
    independent_groups: int = 0
    assumed_by: int = 0          # documents that rely on it without testing it
    critiqued_by: int = 0        # arguments against it without new experiments
    depended_on_by: int = 0      # claims (transitively) that presuppose it
    concepts: list[str] = field(default_factory=list)

    @property
    def load(self) -> int:
        return self.assumed_by + self.depended_on_by

    @property
    def fragile_foundation(self) -> bool:
        return self.load >= 2 and self.status in WEAK_STATUSES


def _independent_groups(doc_ids: set[int], authors: dict[int, set[int]]) -> int:
    """Count clusters of documents connected by shared authors (union-find)."""
    parent = {d: d for d in doc_ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    docs = list(doc_ids)
    for i, a in enumerate(docs):
        for b in docs[i + 1:]:
            if authors.get(a, set()) & authors.get(b, set()):
                parent[find(a)] = find(b)
    return len({find(d) for d in docs})


def claim_health(conn) -> dict[int, ClaimHealth]:
    authors: dict[int, set[int]] = defaultdict(set)
    for r in conn.execute("SELECT document_id, author_id FROM document_authors"):
        authors[r[0]].add(r[1])

    evidence = defaultdict(list)
    for r in conn.execute("SELECT claim_id, document_id, relation, strength FROM claim_evidence"):
        # a paper that proposes a claim and presents evidence for it is that claim's first test
        relation = "supports" if r[2] == "proposes" and r[3] else r[2]
        evidence[r[0]].append((r[1], relation, r[3]))

    deps = defaultdict(set)  # depends_on -> {claims that depend on it}
    for r in conn.execute("SELECT claim_id, depends_on_id FROM claim_dependencies"):
        deps[r[1]].add(r[0])

    concepts = defaultdict(list)
    for r in conn.execute("SELECT cc.claim_id, c.slug FROM claim_concepts cc JOIN concepts c ON c.id = cc.concept_id"):
        concepts[r[0]].append(r[1])

    out = {}
    for row in conn.execute("SELECT id, slug, statement, status_override FROM claims"):
        ev = evidence[row["id"]]
        pos = {d for d, rel, _ in ev if rel in POSITIVE}
        neg = {d for d, rel, _ in ev if rel in NEGATIVE}
        mixed = {d for d, rel, _ in ev if rel == "mixed"}
        # weak evidence counts as support, but not toward independent replication
        solid_pos = {d for d, rel, strength in ev if rel in POSITIVE and strength != "weak"}
        groups = _independent_groups(solid_pos, authors) if solid_pos else 0

        if (pos and neg) or (mixed and not pos):
            derived = "contested"
        elif neg:
            derived = "refuted"
        elif groups >= 2:
            derived = "replicated"
        elif len(pos) >= 2:
            derived = "supported"
        elif pos:
            derived = "single_source"
        else:
            derived = "untested"

        # transitive dependents: everything whose truth leans on this claim
        seen, stack = set(), list(deps[row["id"]])
        while stack:
            c = stack.pop()
            if c not in seen:
                seen.add(c)
                stack.extend(deps[c])

        out[row["id"]] = ClaimHealth(
            id=row["id"], slug=row["slug"], statement=row["statement"],
            status=row["status_override"] or derived, derived_status=derived,
            positive_docs=len(pos), negative_docs=len(neg), mixed_docs=len(mixed), independent_groups=groups,
            assumed_by=len({d for d, rel, _ in ev if rel == "assumes"}),
            critiqued_by=len({d for d, rel, _ in ev if rel == "critiques"}),
            depended_on_by=len(seen), concepts=concepts[row["id"]],
        )
    return out


def concept_descendants(conn) -> dict[int, set[int]]:
    children = defaultdict(set)
    for r in conn.execute("SELECT child_id, parent_id FROM concept_relations"):
        children[r[1]].add(r[0])
    result = {}
    for (cid,) in conn.execute("SELECT id FROM concepts"):
        seen, stack = {cid}, [cid]
        while stack:
            for ch in children[stack.pop()]:
                if ch not in seen:
                    seen.add(ch)
                    stack.append(ch)
        result[cid] = seen
    return result


def concept_health(conn, claims: dict[int, ClaimHealth]) -> list[dict]:
    desc = concept_descendants(conn)
    claim_concepts = defaultdict(set)
    for r in conn.execute("SELECT claim_id, concept_id FROM claim_concepts"):
        claim_concepts[r[1]].add(r[0])
    doc_concepts = defaultdict(set)
    for r in conn.execute("SELECT document_id, concept_id FROM document_concepts"):
        doc_concepts[r[1]].add(r[0])
    notes = defaultdict(lambda: defaultdict(int))
    for r in conn.execute("SELECT target_id, kind FROM annotations WHERE target_type = 'concept'"):
        notes[r[0]][r[1]] += 1

    rows = []
    for c in conn.execute("SELECT id, slug, name FROM concepts"):
        cids = set().union(*(claim_concepts[d] for d in desc[c["id"]]))
        docs = set().union(*(doc_concepts[d] for d in desc[c["id"]]))
        statuses = defaultdict(int)
        for cid in cids:
            statuses[claims[cid].status] += 1
        n = len(cids)
        solid = statuses["replicated"] + statuses["supported"]
        rows.append({
            "slug": c["slug"], "name": c["name"], "documents": len(docs), "claims": n,
            "statuses": dict(statuses),
            "solid_share": solid / n if n else None,
            "fragile": sum(claims[cid].fragile_foundation for cid in cids),
            "next_steps": notes[c["id"]]["next_step"], "dead_ends": notes[c["id"]]["dead_end"],
        })
    return sorted(rows, key=lambda r: (-r["claims"], r["slug"]))
