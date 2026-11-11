"""Sorting claims into levels: paradigm, thesis (a broad claim) and finding (one experiment).

A finding hangs under the thesis it bears on, with a stance (supports / contradicts / qualifies /
applies). A thesis names the paradigms it rests on and the theses it depends on. A thesis may
have a single finding today: later papers are expected to add evidence to it.

Two stages, like consolidation:

1. `sort_claims()` walks the map in chunks of related claims and asks a model to sort each
   chunk. Chunks run one after another, and each sees the paradigms and theses earlier chunks
   settled on, so different areas share them instead of inventing their own. Results go to
   data/structure/sort.jsonl; the database is not touched, and a rerun continues where it stopped.
2. `apply()` writes the levels and links to the database (see its docstring).
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from typing import Callable, Optional

from . import db, extract

DIR = db.ROOT / "data" / "structure"
SORT_FILE = DIR / "sort.jsonl"
CHUNK = 140
STANCES = {"supports", "contradicts", "qualifies", "applies"}
LEVELS = {"paradigm", "thesis"}

INSTRUCTIONS = """\
You are organising the claims of a research map of LLM interpretability and alignment. Readers use
the map to learn the field, to see which ideas rest on which assumptions, and to find foundations
that need shoring up. The claims are a flat list, and most restate one paper's result. Sort them
into three levels:

- "paradigm": a framing assumption or worldview that much work takes for granted and builds on,
  usually without testing it directly (e.g. "features are directions in activation space").
- "thesis": a general claim that independent papers could test, support or contradict.
- "finding": the result of one experiment or one paper: a particular model, dataset, metric or
  setting. A methodological claim about one paper's own procedure is also a finding.

<higher> lists paradigms and theses already settled on in earlier parts of the map
(id | level | statement). <claims> lists the claims to sort now
(id | type | documents with evidence | claims that depend on it | statement).

Return JSON:
{"promote":    [{"id": claim id, "level": "paradigm" | "thesis", "rests_on": [ids], "depends_on": [ids]}],
 "new":        [{"id": "n1", "level": "paradigm" | "thesis", "statement": ..., "rests_on": [ids], "depends_on": [ids]}],
 "findings":   [{"id": claim id, "under": id or null, "stance": "supports" | "contradicts" | "qualifies" | "applies"}],
 "duplicates": [{"id": claim id, "same_as": id}]}

Rules:
- Every id in <claims> appears exactly once: in "promote", in "findings" or in "duplicates".
- Use what is in <higher> first. File a finding under an existing paradigm or thesis whenever one
  fits, and do not promote or write a claim that says the same thing as one already there.
- "promote" an existing claim when its wording is already at the right level. Write a "new"
  statement only when at least two findings share it and nothing existing captures them. A new
  thesis must stand alone, be testable, and be broader than each finding under it.
- A promoted thesis may have one finding, or none yet, if other papers could plausibly test it:
  the map is growing and evidence will be added later. Do not promote a claim that only makes
  sense for one paper's setup.
- Paradigms are rare: add one only for an assumption that many theses share.
- Stance: "supports" and "contradicts" are tests of the thesis; "qualifies" narrows or bounds it
  (it holds only under some condition); "applies" relies on it as a tool or premise without
  testing it. A finding against the thesis must be "contradicts", never folded in as support.
- A finding that fits nothing gets "under": null. Do not force a fit.
- "rests_on" lists paradigms the claim presupposes; "depends_on" lists theses that must hold for
  it to make sense. Both may refer to ids from <higher>, <claims> or "new".
- "duplicates" are claims that say the same thing as another claim or an entry in <higher>.
Output the JSON object and nothing else.
"""


def chunks(conn) -> list[list[int]]:
    """Claims grouped by a home concept (the most specific of their concepts that is big enough
    to give context), largest areas first, cut into pieces of at most CHUNK."""
    size = dict(conn.execute("SELECT concept_id, COUNT(*) FROM claim_concepts GROUP BY 1").fetchall())
    mine = defaultdict(list)
    for claim_id, concept_id in conn.execute("SELECT claim_id, concept_id FROM claim_concepts"):
        mine[claim_id].append(concept_id)
    load = dict(conn.execute("SELECT depends_on_id, COUNT(*) FROM claim_dependencies GROUP BY 1").fetchall())
    home = defaultdict(list)
    for claim_id, ks in mine.items():
        big = [k for k in ks if size[k] >= 25]
        home[min(big, key=lambda k: (size[k], k)) if big else max(ks, key=lambda k: (size[k], k))].append(claim_id)
    out, cur = [], []
    for k in sorted(home, key=lambda k: (-len(home[k]), k)):
        for cid in sorted(home[k], key=lambda c: (-load.get(c, 0), c)):
            cur.append(cid)
            if len(cur) == CHUNK:
                out.append(cur)
                cur = []
    if cur:
        out.append(cur)
    return out


def load_results() -> list[dict]:
    return [json.loads(l) for l in SORT_FILE.open(encoding="utf-8")] if SORT_FILE.exists() else []


def registry(conn, results: list[dict]) -> dict[str, dict]:
    """Paradigms and theses settled so far: global id -> {level, statement, concepts}.
    Promoted claims keep the id c<claim id>; new statements get n<chunk>_<k>."""
    concepts = defaultdict(set)
    for claim_id, concept_id in conn.execute("SELECT claim_id, concept_id FROM claim_concepts"):
        concepts[f"c{claim_id}"].add(concept_id)
    stmt = {f"c{r[0]}": r[1] for r in conn.execute("SELECT id, statement FROM claims")}
    reg = {}
    for res in results:
        for p in res["promote"]:
            reg[p["id"]] = {"level": p["level"], "statement": stmt[p["id"]], "concepts": set(concepts[p["id"]])}
        for n in res["new"]:
            reg[n["id"]] = {"level": n["level"], "statement": n["statement"], "concepts": set()}
        for f in res["findings"]:   # a thesis inherits the concepts of what is filed under it
            if f["under"] in reg:
                reg[f["under"]]["concepts"] |= concepts[f["id"]]
    return reg


def validate(ids: list[str], known: set[str], raw: dict, tag: str) -> dict:
    """Keep only well-formed entries, rename new ids to be unique across chunks, and file any
    claim the model skipped or mangled as an unplaced finding."""
    rename = {n.get("id"): f"n{tag}_{i}" for i, n in enumerate(raw.get("new") or []) if n.get("statement")}
    fix = lambda x: rename.get(x, x)
    idset = set(ids)
    promoted = {p["id"] for p in raw.get("promote") or [] if p.get("id") in idset and p.get("level") in LEVELS}
    valid = known | promoted | set(rename.values())
    refs = lambda xs, self: [fix(x) for x in xs or [] if fix(x) in valid and fix(x) != self]
    out = {"promote": [], "new": [], "findings": [], "duplicates": []}
    seen = set()
    for p in raw.get("promote") or []:
        if p.get("id") in promoted and p["id"] not in seen:
            seen.add(p["id"])
            out["promote"].append({"id": p["id"], "level": p["level"], "rests_on": refs(p.get("rests_on"), p["id"]),
                                   "depends_on": refs(p.get("depends_on"), p["id"])})
    for n in raw.get("new") or []:
        if n.get("id") in rename and n.get("level") in LEVELS:
            nid = rename[n["id"]]
            out["new"].append({"id": nid, "level": n["level"], "statement": " ".join(n["statement"].split()),
                               "rests_on": refs(n.get("rests_on"), nid), "depends_on": refs(n.get("depends_on"), nid)})
    for d in raw.get("duplicates") or []:
        same = fix(d.get("same_as"))
        if d.get("id") in idset and d["id"] not in seen and same != d["id"] and (same in valid or same in idset):
            seen.add(d["id"])
            out["duplicates"].append({"id": d["id"], "same_as": same})
    for f in raw.get("findings") or []:
        if f.get("id") in idset and f["id"] not in seen:
            seen.add(f["id"])
            under = fix(f.get("under"))
            ok = under in valid and f.get("stance") in STANCES
            out["findings"].append({"id": f["id"], "under": under if ok else None, "stance": f["stance"] if ok else None})
    out["findings"] += [{"id": i, "under": None, "stance": None, "skipped": True} for i in ids if i not in seen]
    return out


def sort_claims(conn, *, limit: Optional[int] = None, log: Callable[[str], None] = print) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    results = load_results()
    done = {i for r in results for i in r["ids"]}
    todo = [[c for c in ch if f"c{c}" not in done] for ch in chunks(conn)]
    todo = [ch for ch in todo if ch]
    # resumed runs can leave small remainders; repack so calls stay full
    flat = [c for ch in todo for c in ch]
    todo = [flat[i:i + CHUNK] for i in range(0, len(flat), CHUNK)]
    log(f"{len(done)} claims sorted, {len(flat)} to go in {len(todo)} chunks")
    rows = {r["id"]: r for r in conn.execute(
        """SELECT cl.id, cl.statement, cl.claim_type,
                  (SELECT COUNT(DISTINCT document_id) FROM claim_evidence e WHERE e.claim_id = cl.id) AS docs,
                  (SELECT COUNT(*) FROM claim_dependencies d WHERE d.depends_on_id = cl.id) AS load FROM claims cl""")}
    concepts = defaultdict(set)
    for claim_id, concept_id in conn.execute("SELECT claim_id, concept_id FROM claim_concepts"):
        concepts[claim_id].add(concept_id)
    for n, ch in enumerate(todo[:limit] if limit else todo):
        reg = registry(conn, results)
        area = Counter(k for c in ch for k in concepts[c])
        score = lambda v: sum(area[k] for k in v["concepts"])
        theses = sorted((i for i, v in reg.items() if v["level"] == "thesis" and score(v)), key=lambda i: -score(reg[i]))[:90]
        paradigms = sorted((i for i, v in reg.items() if v["level"] == "paradigm"), key=lambda i: -score(reg[i]))[:30]
        higher = "\n".join(f"{i} | {reg[i]['level']} | {reg[i]['statement']}" for i in paradigms + theses) or "(none yet)"
        lines = "\n".join(f"c{c} | {rows[c]['claim_type']} | {rows[c]['docs']} | {rows[c]['load']} | {rows[c]['statement']}" for c in ch)
        prompt = f"{INSTRUCTIONS}\n<higher>\n{higher}\n</higher>\n<claims>\n{lines}\n</claims>\n"
        try:
            raw = extract.run_claude_cli(prompt, telemetry={"purpose": "structure-sort"})
            parsed = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
        except (extract.UsageLimitError, extract.NotLoggedInError) as e:
            log(f"stopped: {type(e).__name__}; rerun to continue")
            return
        except (json.JSONDecodeError, AttributeError):
            log(f"chunk {n + 1}: unreadable answer, skipped for now")
            continue
        except RuntimeError as e:   # the CLI process died (e.g. the machine slept); progress so far is saved
            log(f"stopped: {str(e)[:120]}; rerun to continue")
            return
        ids = [f"c{c}" for c in ch]
        res = {"ids": ids, **validate(ids, set(reg), parsed, str(len(results)))}
        with SORT_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(res, ensure_ascii=False) + "\n")
        results.append(res)
        placed = sum(1 for x in res["findings"] if x["under"])
        log(f"chunk {n + 1}/{len(todo)}: {len(res['promote'])} promoted, {len(res['new'])} new, {placed} findings placed, "
            f"{len(res['findings']) - placed} unplaced, {len(res['duplicates'])} duplicates "
            f"({sum(1 for x in res['findings'] if x.get('skipped'))} skipped by the model)")
    log("done")


APPLIED = DIR / "applied.json"


def apply(conn, *, log: Callable[[str], None] = print) -> dict:
    """Write the sorted levels and links to the database. Safe to rerun as more chunks finish.

    - promoted claims get their level; statements written by the model become new claims,
      tagged with the concepts most of their findings share;
    - every other sorted claim becomes a finding, linked to its parent with its stance;
    - "rests on" and "depends on" become claim dependencies;
    - claims flagged as duplicates are left untouched and listed in duplicates.json for review.
    Call on a connection opened with db.connect() after db.init(); the caller makes the backup.
    """
    results = load_results()
    if PLACE_FILE.exists():   # second-pass placements fill in findings the first pass left loose
        later = {f["id"]: f for l in PLACE_FILE.open(encoding="utf-8") for f in json.loads(l)["findings"]}
        for res in results:
            res["findings"] = [later.get(f["id"], f) if not f["under"] else f for f in res["findings"]]
    made = json.loads(APPLIED.read_text()) if APPLIED.exists() else {}   # new id -> claim id
    alive = {r[0] for r in conn.execute("SELECT id FROM claims")}
    made = {k: v for k, v in made.items() if v in alive}
    types = dict(conn.execute("SELECT id, claim_type FROM claims").fetchall())
    concepts = defaultdict(set)
    for claim_id, concept_id in conn.execute("SELECT claim_id, concept_id FROM claim_concepts"):
        concepts[claim_id].add(concept_id)
    under = defaultdict(list)
    for res in results:
        for f in res["findings"]:
            if f["under"]:
                under[f["under"]].append(int(f["id"][1:]))

    def cid(x):
        return int(x[1:]) if x[0] == "c" else made.get(x)

    stats = Counter()
    for res in results:
        for n in res["new"]:
            if n["id"] in made:
                continue
            kids = under[n["id"]]
            kind = Counter(types[k] for k in kids if k in types).most_common(1)
            new_id = conn.execute("INSERT INTO claims (slug, statement, claim_type, level) VALUES (?, ?, ?, ?)",
                                  (db._unique_slug(conn, "claims", db.slugify(n["statement"])), n["statement"],
                                   kind[0][0] if kind else "hypothesis", n["level"])).lastrowid
            shared = Counter(k for kid in kids for k in concepts[kid])
            keep = [k for k, v in shared.items() if v * 2 >= len(kids)] or [k for k, _ in shared.most_common(1)]
            conn.executemany("INSERT OR IGNORE INTO claim_concepts VALUES (?, ?)", [(new_id, k) for k in keep])
            made[n["id"]] = new_id
            stats["new " + n["level"]] += 1
    for res in results:
        for p in res["promote"]:
            conn.execute("UPDATE claims SET level = ? WHERE id = ?", (p["level"], cid(p["id"])))
            stats[p["level"]] += 1
        for f in res["findings"]:
            conn.execute("UPDATE claims SET level = 'finding' WHERE id = ?", (cid(f["id"]),))
            parent = cid(f["under"]) if f["under"] else None
            if parent in alive or parent in made.values():
                conn.execute("INSERT OR REPLACE INTO claim_links VALUES (?, ?, ?, ?)",
                             (cid(f["id"]), parent, f["stance"], f"structure-sort:{extract.MODEL}"))
                stats["finding placed"] += 1
            else:
                stats["finding unplaced"] += 1
        for item in res["promote"] + res["new"]:
            for key, note in (("rests_on", "rests on this paradigm"), ("depends_on", "depends on this thesis")):
                for target in item[key]:
                    a, b = cid(item["id"]), cid(target)
                    if a and b and a != b:
                        conn.execute("INSERT OR IGNORE INTO claim_dependencies VALUES (?, ?, ?)", (a, b, note))
                        stats["dependency"] += 1
    stats["cycles broken"] = db.break_dependency_cycles(conn)
    dups = [d for res in results for d in res["duplicates"]]
    (DIR / "duplicates.json").write_text(json.dumps(dups, indent=1), encoding="utf-8")
    stats["duplicates listed"] = len(dups)
    conn.commit()
    APPLIED.write_text(json.dumps(made))
    log(str(dict(stats)))
    return dict(stats)


PLACE_FILE = DIR / "place.jsonl"

PLACE_INSTRUCTIONS = """\
You are filing single-experiment findings in a research map of LLM interpretability and alignment.
<higher> lists the map's paradigms and broad claims (id | level | statement). <findings> lists
findings that were not filed under any of them when first sorted, because the list was shorter
then (id | statement).

For each finding, choose the entry in <higher> it bears on, with a stance:
"supports" or "contradicts" (it tests the claim), "qualifies" (it narrows or bounds it), or
"applies" (it relies on the claim as a tool or premise without testing it).
Leave a finding out when nothing fits: most one-off results belong nowhere, and a wrong parent is
worse than none. Return JSON {"findings": [{"id": ..., "under": ..., "stance": ...}]} and nothing else.
"""


def place_unplaced(conn, *, log: Callable[[str], None] = print) -> None:
    """Second pass: offer findings left without a parent the full list of paradigms and theses."""
    results = load_results()
    reg = registry(conn, results)
    tried = {i for l in (PLACE_FILE.open(encoding="utf-8") if PLACE_FILE.exists() else []) for i in json.loads(l)["ids"]}
    loose = [f["id"] for res in results for f in res["findings"] if not f["under"] and f["id"] not in tried]
    stmt = {f"c{r[0]}": r[1] for r in conn.execute("SELECT id, statement FROM claims")}
    concepts = defaultdict(set)
    for claim_id, concept_id in conn.execute("SELECT claim_id, concept_id FROM claim_concepts"):
        concepts[f"c{claim_id}"].add(concept_id)
    loose.sort(key=lambda i: sorted(concepts[i]))   # keeps findings from one area together
    log(f"{len(loose)} unplaced findings to offer")
    for start in range(0, len(loose), CHUNK):
        ch = loose[start:start + CHUNK]
        area = Counter(k for c in ch for k in concepts[c])
        score = lambda i: sum(area[k] for k in reg[i]["concepts"])
        higher = sorted((i for i in reg if score(i)), key=lambda i: -score(i))[:160]
        prompt = (f"{PLACE_INSTRUCTIONS}\n<higher>\n" + "\n".join(f"{i} | {reg[i]['level']} | {reg[i]['statement']}" for i in higher)
                  + "\n</higher>\n<findings>\n" + "\n".join(f"{i} | {stmt[i]}" for i in ch) + "\n</findings>\n")
        try:
            raw = extract.run_claude_cli(prompt, telemetry={"purpose": "structure-place"})
            parsed = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
        except (extract.UsageLimitError, extract.NotLoggedInError, RuntimeError) as e:
            log(f"stopped: {str(e)[:120] or type(e).__name__}; rerun to continue")
            return
        except (json.JSONDecodeError, AttributeError):
            log("unreadable answer, skipped for now")
            continue
        seen, placed = set(), []
        for f in parsed.get("findings") or []:
            if f.get("id") in ch and f["id"] not in seen and f.get("under") in reg and f.get("stance") in STANCES:
                seen.add(f["id"])
                placed.append({"id": f["id"], "under": f["under"], "stance": f["stance"]})
        with PLACE_FILE.open("a", encoding="utf-8") as out:
            out.write(json.dumps({"ids": ch, "findings": placed}) + "\n")
        log(f"{min(start + CHUNK, len(loose))}/{len(loose)}: placed {len(placed)} of {len(ch)}")
    log("done")


RECONCILE_FILE = DIR / "reconcile.json"

RECONCILE_INSTRUCTIONS = """\
You are cleaning up the top two levels of a research map of LLM interpretability and alignment.
The levels were assigned area by area, so the same idea may appear more than once, and the
"paradigm" level has grown too large.

- "paradigm": a framing assumption or worldview that a large body of work takes for granted and
  builds on, usually without testing it directly. A field has few of these. The assumption that
  one particular metric or procedure is a useful proxy is NOT a paradigm: it is a thesis.
- "thesis": a general claim that independent papers could test, support or contradict.

Each line is: id | level | findings filed under it | claims depending on it | statement.

Return JSON:
{"merge":  [{"keep": id, "drop": [ids], "why": at most 12 words}],
 "relevel": [{"id": id, "level": "paradigm" | "thesis", "why": at most 12 words}],
 "out_of_scope": [{"id": id, "why": at most 12 words}],
 "rests_on": [{"id": thesis id, "paradigms": [paradigm ids]}]}

Rules:
- "merge" only entries that assert the same thing, so that evidence for one is evidence for the
  other. A narrower version, a special case, a different model family or modality, or the opposite
  direction is NOT the same claim. Keep the clearer, more general wording, preferring the entry
  with more findings. When unsure, do not merge.
- "relevel" only entries whose level is wrong. Expect most current paradigms about a metric,
  proxy, benchmark or procedure to become theses, leaving roughly 8 to 15 paradigms.
- "out_of_scope": entries that are not about neural-network interpretability or AI alignment.
- "rests_on": for each thesis marked with * (it names no paradigm yet), the paradigms it
  presupposes, using paradigm ids as they will stand after your relevelling. Omit a thesis that
  rests on none of them.
Output the JSON object and nothing else.
"""


def reconcile(conn, *, log: Callable[[str], None] = print) -> dict:
    """One call over every paradigm and thesis: propose merges, level fixes and missing paradigm
    links. Saves the proposal to reconcile.json; nothing in the database changes."""
    rows = conn.execute(
        """SELECT c.id, c.level, c.statement,
                  (SELECT COUNT(*) FROM claim_links l WHERE l.parent_id = c.id) AS findings,
                  (SELECT COUNT(*) FROM claim_dependencies d WHERE d.depends_on_id = c.id) AS load,
                  (SELECT MIN(k.name) FROM claim_concepts x JOIN concepts k ON k.id = x.concept_id WHERE x.claim_id = c.id) AS area,
                  EXISTS (SELECT 1 FROM claim_dependencies d JOIN claims p ON p.id = d.depends_on_id
                          WHERE d.claim_id = c.id AND p.level = 'paradigm') AS grounded
           FROM claims c WHERE c.level IN ('paradigm', 'thesis') ORDER BY c.level, area, c.id""").fetchall()
    lines = "\n".join(f"c{r['id']}{'' if r['grounded'] or r['level'] == 'paradigm' else '*'} | {r['level']} | {r['findings']} | {r['load']} | {r['statement']}"
                      for r in rows)
    raw = extract.run_claude_cli(f"{RECONCILE_INSTRUCTIONS}\n<entries>\n{lines}\n</entries>\n", telemetry={"purpose": "structure-reconcile"})
    plan = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
    RECONCILE_FILE.write_text(json.dumps(plan, indent=1, ensure_ascii=False), encoding="utf-8")
    log(f"{len(rows)} entries: {len(plan.get('merge') or [])} merges, {len(plan.get('relevel') or [])} level changes, "
        f"{len(plan.get('out_of_scope') or [])} out of scope, {len(plan.get('rests_on') or [])} paradigm links")
    return plan


def apply_reconcile(conn, *, log: Callable[[str], None] = print) -> dict:
    """Apply reconcile.json: merge duplicates (old slugs become aliases), fix levels, add paradigm
    links. Out-of-scope entries are only reported. The caller makes the backup."""
    plan = json.loads(RECONCILE_FILE.read_text(encoding="utf-8"))
    slug = lambda x: (conn.execute("SELECT slug FROM claims WHERE id = ?", (int(x[1:]),)).fetchone() or [None])[0]
    top = lambda x: conn.execute("SELECT 1 FROM claims WHERE id = ? AND level IN ('paradigm','thesis')", (int(x[1:]),)).fetchone()
    stats = Counter()
    for r in plan.get("relevel") or []:
        if top(r["id"]) and r.get("level") in LEVELS:
            conn.execute("UPDATE claims SET level = ? WHERE id = ?", (r["level"], int(r["id"][1:])))
            stats["relevelled"] += 1
    for m in plan.get("merge") or []:
        for d in m.get("drop") or []:
            if d != m["keep"] and top(d) and top(m["keep"]):
                db.merge(conn, "claim", slug(d), slug(m["keep"]))
                stats["merged"] += 1
    for r in plan.get("rests_on") or []:
        for p in r.get("paradigms") or []:
            a, b = int(r["id"][1:]), int(p[1:])
            if a != b and conn.execute("SELECT 1 FROM claims WHERE id = ? AND level = 'paradigm'", (b,)).fetchone() \
                    and conn.execute("SELECT 1 FROM claims WHERE id = ?", (a,)).fetchone():
                stats["paradigm links"] += conn.execute("INSERT OR IGNORE INTO claim_dependencies VALUES (?, ?, ?)",
                                                        (a, b, "rests on this paradigm")).rowcount
    stats["cycles broken"] = db.break_dependency_cycles(conn)
    conn.commit()
    log(str(dict(stats)))
    return dict(stats)
