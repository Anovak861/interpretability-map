"""Pilot: propose a claim hierarchy and a concept neighbourhood for one concept (read-only).

    python scripts/pilot_structure.py linear-representation-hypothesis

Two model calls. The first sorts the concept's claims into levels (paradigm / thesis / finding),
attaches findings to the thesis they bear on, and names the paradigms each thesis rests on. The
second sorts neighbouring concepts into parts, foundations, extensions and related ideas.
Nothing is written to the database; the proposal goes to data/pilot/<slug>.json for review.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from interp_map import extract  # noqa: E402

OUT = ROOT / "data" / "pilot"

CLAIMS = """\
You are organising the claims of one area of a research map of LLM interpretability and alignment.
Readers use the map to learn the field, to see which ideas rest on which assumptions, and to find
foundations that need shoring up. Right now the claims are a flat list; most restate one paper's
result. Sort them into three levels:

- "paradigm": a framing assumption or worldview that much work in the area takes for granted and
  builds on, usually without testing it directly (e.g. "features are directions in activation space").
- "thesis": a general claim that several independent papers could test, support or contradict.
- "finding": the result of one experiment or one paper: a particular model, dataset, metric or
  setting. A methodological claim about one paper's own procedure is also a finding.

Each line below is: id | type | documents with evidence | claims that depend on it | statement.

Return JSON:
{"paradigms": [{"id": "P1", "statement": ..., "from": existing claim id or null}],
 "theses":    [{"id": "T1", "statement": ..., "from": existing claim id or null,
                "rests_on": [paradigm ids], "depends_on": [other thesis ids]}],
 "findings":  [{"id": existing claim id, "thesis": thesis id or null,
                "stance": "supports" | "contradicts" | "qualifies" | "applies"}],
 "duplicates": [{"id": existing claim id, "same_as": existing claim id}]}

Rules:
- Every existing claim id appears exactly once: as the "from" of a paradigm or thesis, as a
  finding, or as a duplicate. Reuse an existing claim's wording ("from") when it is already at the
  right level; write a new statement (from null) only when at least two findings share it.
- Aim for few paradigms (2-6) and a thesis count around a fifth of the claims or fewer. A thesis
  must stand alone, be testable, and be broader than each finding under it.
- Stance: "supports" and "contradicts" are tests of the thesis; "qualifies" narrows or bounds it
  (holds only under some condition); "applies" uses the thesis as a tool without testing it.
  A finding against the thesis must be "contradicts", never folded in as support.
- A finding that fits no thesis gets thesis null. Do not force a fit.
- "rests_on" lists paradigms the thesis presupposes; "depends_on" lists theses that must hold for
  it to make sense.
Output the JSON object and nothing else.
"""

CONCEPTS = """\
You are organising the concepts around one focus concept in a research map of LLM interpretability
and alignment, for a page that shows a reader what the focus concept is built on, what has been
built on it, and what sits beside it. Sort every listed concept into exactly one group:

- "part": a component, special case or sub-question of the focus concept (belongs under it).
- "foundation": an idea, method or assumption the focus concept needs in order to make sense.
- "extension": something that builds on, generalises or challenges the focus concept.
- "tool": a method, benchmark or dataset used to study the focus concept, but not specific to it.
- "related": a neighbouring idea at a similar level, with no dependence either way.
- "unrelated": listed only because some paper mentions both.

Each line is: id | kind | claims | current link to focus | name: description.
Return JSON {"concepts": [{"id": ..., "group": ..., "why": at most 12 words}]} and nothing else.
"""


def ask(prompt: str, purpose: str) -> dict:
    raw = extract.run_claude_cli(prompt, telemetry={"purpose": purpose})
    return json.loads(re.search(r"\{.*\}", raw, re.S).group(0))


def main():
    slug = sys.argv[1]
    conn = sqlite3.connect(f"file:{(ROOT / 'data' / 'interp_map.db').as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    focus = conn.execute("SELECT * FROM concepts WHERE slug = ?", (slug,)).fetchone()
    claims = conn.execute(
        """SELECT cl.id, cl.slug, cl.statement, cl.claim_type, cl.scope,
                  (SELECT COUNT(DISTINCT document_id) FROM claim_evidence e WHERE e.claim_id = cl.id) AS docs,
                  (SELECT COUNT(*) FROM claim_dependencies d WHERE d.depends_on_id = cl.id) AS load,
                  (SELECT GROUP_CONCAT(DISTINCT relation) FROM claim_evidence e WHERE e.claim_id = cl.id) AS relations
           FROM claims cl JOIN claim_concepts x ON x.claim_id = cl.id WHERE x.concept_id = ?
           ORDER BY load DESC, docs DESC""", (focus["id"],)).fetchall()
    lines = "\n".join(f"c{c['id']} | {c['claim_type']} | {c['docs']} | {c['load']} | {c['statement']}" for c in claims)
    print(f"{focus['name']}: {len(claims)} claims", flush=True)
    structure = ask(f"{CLAIMS}\nArea: {focus['name']}. {focus['description'] or ''}\n<claims>\n{lines}\n</claims>\n", "pilot-claims")

    # neighbours: current parents and children, plus concepts sharing the most claims
    near = {}
    for r in conn.execute("SELECT k.*, 'child' AS link FROM concept_relations r JOIN concepts k ON k.id = r.child_id WHERE r.parent_id = ?", (focus["id"],)):
        near[r["id"]] = r
    for r in conn.execute("SELECT k.*, 'parent' AS link FROM concept_relations r JOIN concepts k ON k.id = r.parent_id WHERE r.child_id = ?", (focus["id"],)):
        near[r["id"]] = r
    for r in conn.execute(
            """SELECT k.*, 'shares ' || COUNT(*) || ' claims' AS link FROM claim_concepts a JOIN claim_concepts b ON a.claim_id = b.claim_id
               JOIN concepts k ON k.id = b.concept_id WHERE a.concept_id = ? AND b.concept_id != ?
               GROUP BY k.id HAVING COUNT(*) >= 4 ORDER BY COUNT(*) DESC LIMIT 60""", (focus["id"], focus["id"])):
        near.setdefault(r["id"], r)
    n_claims = dict(conn.execute("SELECT concept_id, COUNT(*) FROM claim_concepts GROUP BY 1").fetchall())
    clines = "\n".join(f"k{k['id']} | {k['kind']} | {n_claims.get(k['id'], 0)} | {k['link']} | {k['name']}: {(k['description'] or '')[:160]}"
                       for k in near.values())
    print(f"{len(near)} neighbouring concepts", flush=True)
    hood = ask(f"{CONCEPTS}\nFocus concept: {focus['name']}. {focus['description'] or ''}\n<concepts>\n{clines}\n</concepts>\n", "pilot-concepts")

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{slug}.json").write_text(json.dumps({
        "focus": {"slug": slug, "name": focus["name"], "description": focus["description"]},
        "claims": {f"c{c['id']}": {k: c[k] for k in ("slug", "statement", "claim_type", "scope", "docs", "load", "relations")} for c in claims},
        "concepts": {f"k{k['id']}": {"slug": k["slug"], "name": k["name"], "kind": k["kind"], "description": k["description"],
                                     "claims": n_claims.get(k["id"], 0), "link": k["link"]} for k in near.values()},
        "structure": structure, "neighbourhood": hood}, indent=1, ensure_ascii=False), encoding="utf-8")
    print("saved", OUT / f"{slug}.json", flush=True)


if __name__ == "__main__":
    main()
