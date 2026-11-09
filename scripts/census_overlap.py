"""Compare the arXiv keyword set with the citation crawl (no model calls).

    python scripts/census_overlap.py

Counts arXiv CS papers matching the language-model keyword labels, and how many the crawl reached,
by year of first submission. Measures crawl coverage of keyword-matching arXiv papers, which is a
second recall check independent of LessWrong links.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from interp_map import census  # noqa: E402

C = ROOT / "data" / "census"
LM_LABELS = {"interp", "align", "both"}


def main():
    arxiv = {}
    for l in (C / "arxiv_cs.jsonl").open(encoding="utf-8"):
        r = json.loads(l)
        arxiv[r["id"]] = r
    crawl = set()
    for l in (C / "openalex_works.jsonl").open(encoding="utf-8"):
        a = json.loads(l).get("arxiv")
        if a:
            crawl.add(a.split("v")[0])
    tot, hit = Counter(), Counter()
    labels = Counter()
    for i, r in arxiv.items():
        label = census.classify(r["title"], r["abstract"])
        if label in LM_LABELS:
            y = (r["created"] or "0000")[:4]
            tot[y] += 1
            hit[y] += i in crawl
            labels[label] += 1
    print(f"arXiv CS papers: {len(arxiv)}; crawl arXiv ids: {len(crawl)}; keyword-matching: {sum(tot.values())} {dict(labels)}")
    print(f"crawl coverage: {sum(hit.values())}/{sum(tot.values())} = {sum(hit.values()) / max(sum(tot.values()), 1):.1%}")
    for y in sorted(tot):
        print(f"  {y}: {tot[y]:6} matching, {hit[y]:6} in crawl ({hit[y] / tot[y]:.0%})")


if __name__ == "__main__":
    main()
