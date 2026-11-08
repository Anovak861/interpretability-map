"""Size the interpretability / alignment literature from the census crawls, with an LLM-judged
sample to measure the keyword classifier's precision and the share of works reporting experiments.

    python scripts/census_estimate.py judge [--n 400]     # spends a few plan calls
    python scripts/census_estimate.py report              # no model calls

Inputs (data/census/): arxiv_cs.jsonl (scripts/census_arxiv.py), openalex_works.jsonl
(scripts/census_citations.py), lesswrong_ai.jsonl (scripts/census_lesswrong.py).
Judgments are cached in judgments.jsonl so `report` can be rerun freely.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from interp_map import census, db, extract  # noqa: E402

C = ROOT / "data" / "census"
JUDGED = C / "judgments.jsonl"

JUDGE = """\
You are sizing the research literature on (a) mechanistic interpretability of neural networks,
especially language models, and (b) alignment / safety of language-model-based AI systems
(RLHF and its failure modes, deception, sycophancy, jailbreaks and refusals, monitoring and
control, evaluations of dangerous or misaligned behavior, scalable oversight, etc.).
For each item decide:
- scope: "core" (squarely in (a) or (b)), "adjacent" (uses interpretability or safety only as a
  side aspect, e.g. a general NLP or explainability-for-end-users paper), or "out".
- kind: "experiment" (reports new empirical results), "method" (new technique, evaluated
  empirically), "benchmark", "theory", "survey", "position" (argument / agenda / essay without
  new results), or "other".
Output JSON {"items": [{"id": ..., "scope": ..., "kind": ...}]} and nothing else.
"""


def load(name):
    p = C / name
    return [json.loads(l) for l in p.open(encoding="utf-8")] if p.exists() else []


def arxiv_records():
    seen = {}
    for r in load("arxiv_cs.jsonl"):
        seen[r["id"]] = r
    return list(seen.values())


def judged():
    return {r["key"]: r for r in load("judgments.jsonl")}


def judge(items: list[dict]) -> None:
    done = judged()
    items = [it for it in items if it["key"] not in done]
    with JUDGED.open("a", encoding="utf-8") as f:
        for i in range(0, len(items), 40):
            batch = items[i:i + 40]
            payload = [{"id": j, "title": it["title"], "text": (it["text"] or "")[:900]} for j, it in enumerate(batch)]
            raw = extract.run_claude_cli(f"{JUDGE}\n<items>\n{json.dumps(payload)}\n</items>\n",
                                         telemetry={"purpose": "census-judge"})
            m = re.search(r"\{.*\}", raw, re.S)
            for res in json.loads(m.group(0))["items"]:
                it = batch[int(res["id"])]
                f.write(json.dumps({"key": it["key"], "stratum": it["stratum"], "scope": res.get("scope"),
                                    "kind": res.get("kind")}) + "\n")
            f.flush()
            print(f"  judged {min(i + 40, len(items))} of {len(items)}", flush=True)


def samples(n: int) -> list[dict]:
    rng = random.Random(0)
    out = []
    arx = arxiv_records()
    by = defaultdict(list)
    for r in arx:
        by[census.classify(r["title"], r["abstract"]) or "none"].append(r)
    # keyword hits per label, and keyword misses among works the citation crawl reached
    for label in ("interp", "align", "both", "interp_nn"):
        for r in rng.sample(by[label], min(n // 4, len(by[label]))):
            out.append({"key": "arxiv:" + r["id"], "stratum": "arxiv:" + label, "title": r["title"], "text": r["abstract"]})
    crawl = [w for w in load("openalex_works.jsonl") if w["how"] != "seed" and w["abstract"]]
    by_c = defaultdict(list)
    for w in crawl:
        by_c[w["label"] or "none"].append(w)
    for label, ws in by_c.items():
        for w in rng.sample(ws, min(n // 4, len(ws))):
            out.append({"key": "oa:" + w["id"], "stratum": "crawl:" + label, "title": w["title"], "text": w["abstract"]})
    # extra draws where the totals are most sensitive: keyword misses, LW interpretability tag
    for w in rng.sample(by_c["none"], min(n // 2, len(by_c["none"]))):
        out.append({"key": "oa:" + w["id"], "stratum": "crawl:none", "title": w["title"], "text": w["abstract"]})
    lwi = [p for p in load("lesswrong_ai.jsonl") if "Interpretability (ML & AI)" in p["tags"]]
    for p in rng.sample(lwi, min(n // 4, len(lwi))):
        out.append({"key": "lw:" + p["id"], "stratum": "lw:interp", "title": p["title"], "text": p["text"]})
    # calibration: how the judge rates documents the map already ingested
    conn = db.connect()
    mine = conn.execute("SELECT slug, title, abstract FROM documents WHERE status = 'extracted' AND length(abstract) > 200").fetchall()
    for r in rng.sample(mine, min(n // 4, len(mine))):
        out.append({"key": "map:" + r["slug"], "stratum": "map", "title": r["title"], "text": r["abstract"]})
    lw = load("lesswrong_ai.jsonl")
    for p in rng.sample(lw, min(n // 4, len(lw))):
        out.append({"key": "lw:" + p["id"], "stratum": "lw:" + ("interp" if "Interpretability (ML & AI)" in p["tags"] else "ai"),
                    "title": p["title"], "text": p["text"]})
    return out


def later_samples(n: int) -> list[dict]:
    """Per-label samples of the crawl tiers added after the first round of judging (2b, 3)."""
    rng = random.Random(1)
    by = defaultdict(list)
    for i, w in enumerate(load("openalex_works.jsonl")):
        tier = census.hop_tier(w["hop"], i)
        if tier in ("2b", "3") and w["abstract"]:
            by[tier, w["label"] or "none"].append({"key": "oa:" + w["id"], "stratum": f"crawl{tier}:{w['label'] or 'none'}",
                                                   "title": w["title"], "text": w["abstract"][:900]})
    out = []
    for (_, label), ws in by.items():   # keyword misses are the largest stratum, so sample them more
        out += rng.sample(ws, min(n * 3 if label == "none" else n, len(ws)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["judge", "judge-later", "report"])
    ap.add_argument("--n", type=int, default=400)
    ap.add_argument("--only", help="stratum prefix to judge (e.g. crawl, lw, arxiv)")
    args = ap.parse_args()
    if args.cmd == "judge-later":
        judge(later_samples(args.n))
        return
    if args.cmd == "judge":
        judge([it for it in samples(args.n) if not args.only or it["stratum"].startswith(tuple(args.only.split(",")))])
        return
    j = judged()
    rates = defaultdict(Counter)
    for r in j.values():
        rates[r["stratum"]][(r["scope"], r["kind"] if r["scope"] == "core" else "-")] += 1
    for s, c in sorted(rates.items()):
        n = sum(c.values())
        core = sum(v for (sc, _), v in c.items() if sc == "core")
        empirical = sum(v for (sc, k), v in c.items() if sc == "core" and k in ("experiment", "method", "benchmark"))
        print(f"{s:16} n={n:4}  core {core / n:5.1%}  core+empirical {empirical / n:5.1%}")
    print(json.dumps({s: dict(Counter({f"{a}/{b}": v for (a, b), v in c.items()})) for s, c in rates.items()}, indent=1))


if __name__ == "__main__":
    main()
