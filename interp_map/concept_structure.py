"""Reorganising the concept graph into a tree plus typed cross-links.

Today every link is "subtopic_of" and most concepts have several parents, so the graph is a
tangle. This sorts each concept's existing links into:

- subtopic_of: its one place in the tree (it is a part or kind of the parent);
- builds_on:   an idea or assumption it needs in order to make sense;
- uses:        a method, tool or benchmark it is studied with;
- related_to:  a neighbouring idea, with no dependence either way.

`plan()` asks a model, in batches, and saves the proposal to data/structure/concepts.jsonl
(resumable; the database is not touched). `apply()` rewrites concept_relations from it.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from typing import Callable

from . import db, extract

FILE = db.ROOT / "data" / "structure" / "concepts.jsonl"
BATCH = 170
TYPES = {"subtopic_of", "builds_on", "uses", "related_to", "drop"}

INSTRUCTIONS = """\
You are tidying the concept graph of a research map of LLM interpretability and alignment, so a
reader can see where each idea sits, what it is built on, and what sits beside it. Each concept
currently lists several "parents" of unclear meaning. For each concept, sort its listed parents:

- "subtopic_of": the concept is a part, special case or kind of that parent. Choose EXACTLY ONE
  such parent, the most specific one that truly contains it: this is its place in the tree.
  (A named method belongs under its method family; a benchmark under what it evaluates.)
- "builds_on": the concept needs that parent's idea or assumption in order to make sense.
- "uses": that parent is a method, tool, model or benchmark the concept is studied with.
- "related_to": a neighbouring idea at a similar level, with no dependence either way.
- "drop": the link is wrong or says nothing (e.g. a distant ancestor already implied by the tree).

Each line is: id | kind | name: description | parents: id=name; id=name ...
Return JSON {"concepts": [{"id": ..., "links": [{"to": parent id, "type": ...}]}]} with every
concept and every one of its listed parents exactly once, and nothing else. A concept with a
single listed parent keeps it as "subtopic_of" unless that is clearly wrong.
"""


def plan(conn, *, log: Callable[[str], None] = print) -> None:
    FILE.parent.mkdir(parents=True, exist_ok=True)
    done = {json.loads(l)["id"] for l in FILE.open(encoding="utf-8")} if FILE.exists() else set()
    concepts = {r["id"]: r for r in conn.execute("SELECT id, name, kind, description FROM concepts")}
    parents = {}
    for child, parent in conn.execute("SELECT child_id, parent_id FROM concept_relations ORDER BY child_id, parent_id"):
        parents.setdefault(child, []).append(parent)
    todo = [c for c in sorted(parents) if f"k{c}" not in done]
    log(f"{len(done)} concepts sorted, {len(todo)} to go")
    for start in range(0, len(todo), BATCH):
        batch = todo[start:start + BATCH]
        lines = "\n".join(
            f"k{c} | {concepts[c]['kind']} | {concepts[c]['name']}: {(concepts[c]['description'] or '')[:140]} | parents: "
            + "; ".join(f"k{p}={concepts[p]['name']}" for p in parents[c]) for c in batch)
        try:
            raw = extract.run_claude_cli(f"{INSTRUCTIONS}\n<concepts>\n{lines}\n</concepts>\n", telemetry={"purpose": "concept-structure"})
            parsed = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
        except (extract.UsageLimitError, extract.NotLoggedInError, RuntimeError) as e:
            log(f"stopped: {str(e)[:120] or type(e).__name__}; rerun to continue")
            return
        except (json.JSONDecodeError, AttributeError):
            log("unreadable answer, skipped for now")
            continue
        got = 0
        with FILE.open("a", encoding="utf-8") as f:
            for item in parsed.get("concepts") or []:
                cid = item.get("id")
                if not (isinstance(cid, str) and cid[1:].isdigit() and int(cid[1:]) in batch) or cid in done:
                    continue
                listed = {f"k{p}" for p in parents[int(cid[1:])]}
                links = {l["to"]: l["type"] for l in item.get("links") or [] if l.get("to") in listed and l.get("type") in TYPES}
                # exactly one tree parent: keep the first the model chose, demote extras to related
                tree = [t for t, kind in links.items() if kind == "subtopic_of"]
                for extra in tree[1:]:
                    links[extra] = "related_to"
                f.write(json.dumps({"id": cid, "links": links, "unsorted": sorted(listed - set(links))}) + "\n")
                done.add(cid)
                got += 1
        log(f"{min(start + BATCH, len(todo))}/{len(todo)}: sorted {got} of {len(batch)}")
    log("done")


def apply(conn, *, log: Callable[[str], None] = print) -> dict:
    """Rewrite concept_relations from the plan. Links the model did not sort are kept as they
    were. A concept left with no tree parent keeps one of its old parents as its place in the
    tree, so nothing becomes a stray root. The caller makes the backup."""
    stats = Counter()
    for line in FILE.open(encoding="utf-8"):
        item = json.loads(line)
        child = int(item["id"][1:])
        links = dict(item["links"])
        if "subtopic_of" not in links.values() and not item["unsorted"]:
            keep = next((t for t, kind in links.items() if kind in ("builds_on", "related_to")), None)
            if keep:
                links[keep] = "subtopic_of"
                stats["tree parent kept by default"] += 1
        for to, kind in links.items():
            parent = int(to[1:])
            conn.execute("DELETE FROM concept_relations WHERE child_id = ? AND parent_id = ?", (child, parent))
            if kind != "drop":
                conn.execute("INSERT OR IGNORE INTO concept_relations VALUES (?, ?, ?)", (child, parent, kind))
            stats[kind] += 1
    conn.commit()
    log(str(dict(stats)))
    return dict(stats)
