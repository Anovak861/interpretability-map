"""Turn the census crawls + judged samples into totals with intervals (no model calls).

    python scripts/census_totals.py   -> prints the tables used in docs/corpus-estimate.md

Each crawl stratum (hop x keyword label) gets the judged share of core-and-empirical works
(Wilson 90% interval); totals are sums over strata. LessWrong uses the judged share of all
AI-tagged posts. Seeds are the map's own documents (judged separately).
"""
from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from interp_map import census  # noqa: E402

C = ROOT / "data" / "census"
EMPIRICAL = {"experiment", "method", "benchmark"}


def wilson(k, n, z=1.645):
    if n == 0:
        return 0, 0, 0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return p, max(0, c - h), c + h


def main():
    works = {}
    for i, l in enumerate((C / "openalex_works.jsonl").open(encoding="utf-8")):
        w = json.loads(l)
        works[w["id"]] = {"hop": w["hop"], "how": w["how"], "label": w["label"], "date": w["date"],
                          "tier": census.hop_tier(w["hop"], i)}
    judged = [json.loads(l) for l in (C / "judgments.jsonl").open(encoding="utf-8")]
    # strata: crawl distance (closer works are more often on-topic) by keyword label
    stratum = lambda w: (w["tier"], w["label"] or "none")
    hits = defaultdict(lambda: [0, 0])
    for j in judged:
        if j["key"].startswith("oa:") and j["key"][3:] in works:
            s = stratum(works[j["key"][3:]])
            hits[s][0] += j["scope"] == "core" and j["kind"] in EMPIRICAL
            hits[s][1] += 1
    size = Counter(stratum(w) for w in works.values() if w["how"] != "seed")
    by_year = defaultdict(Counter)
    for w in works.values():
        if w["how"] != "seed":
            by_year[(w["date"] or "0000")[:4]][stratum(w)] += 1

    print(f"{'stratum':22} {'works':>7} {'judged':>7} {'rate':>6} {'90% CI':>13} {'est.':>6}")
    tot = [0, 0, 0]
    for s in sorted(size, key=str):
        k, n = hits[s]
        p, lo, hi = wilson(k, n)
        est = [size[s] * x for x in (p, lo, hi)]
        tot = [a + b for a, b in zip(tot, est)]
        print(f"{str(s):22} {size[s]:7} {f'{k}/{n}':>7} {p:6.1%} {f'{lo:.1%}-{hi:.1%}':>13} {est[0]:6.0f}")
    print(f"crawl (non-seed) core empirical works: {tot[0]:.0f} (interval {tot[1]:.0f}-{tot[2]:.0f})")

    rate = {s: wilson(*hits[s])[0] for s in size}
    print("\nby publication year (crawl, non-seed):")
    for y in sorted(by_year):
        if y >= "2016":
            print(f"  {y}: {sum(by_year[y][s] * rate.get(s, 0) for s in by_year[y]):6.0f}")

    mp = [j for j in judged if j["stratum"] == "map"]
    k = sum(j["scope"] == "core" and j["kind"] in EMPIRICAL for j in mp)
    print(f"\nmap documents judged core+empirical: {k}/{len(mp)}")

    lw = [json.loads(l) for l in (C / "lesswrong_ai.jsonl").open(encoding="utf-8")]
    lj = [j for j in judged if j["stratum"] == "lw:ai"]
    k = sum(j["scope"] == "core" and j["kind"] in EMPIRICAL for j in lj)
    p, lo, hi = wilson(k, len(lj))
    print(f"LessWrong/AF AI-tagged posts: {len(lw)}; core+empirical {k}/{len(lj)} = {p:.1%} "
          f"({lo:.1%}-{hi:.1%}) -> {len(lw) * p:.0f} ({len(lw) * lo:.0f}-{len(lw) * hi:.0f})")
    years = Counter(x["date"][:4] for x in lw)
    print("  posts by year:", dict(sorted(years.items())))


if __name__ == "__main__":
    main()
