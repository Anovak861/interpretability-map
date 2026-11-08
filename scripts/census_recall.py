"""Recall of the citation crawl, measured on arXiv papers linked from LessWrong/AF posts (no model calls).

    python scripts/census_recall.py

Takes arXiv ids linked by posts with karma >= 10 that are not already in the map, and counts how
many the crawl found, overall and by the paper's arXiv year. Also reports the first-round crawl
(census.FIRST_JUDGED_LINES) so the two can be compared.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from interp_map import census, db  # noqa: E402

C = ROOT / "data" / "census"
ARX = re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})")


def main():
    conn = db.connect()
    mapped = {r[0].split("v")[0] for r in conn.execute("SELECT arxiv_id FROM documents WHERE arxiv_id IS NOT NULL")}
    linked = set()
    for l in (C / "lesswrong_ai.jsonl").open(encoding="utf-8"):
        p = json.loads(l)
        if (p.get("karma") or 0) >= 10:
            linked |= set(ARX.findall((p.get("text") or "") + " " + (p.get("url") or "")))
    linked -= mapped
    first, now = set(), set()
    for i, l in enumerate((C / "openalex_works.jsonl").open(encoding="utf-8")):
        a = json.loads(l).get("arxiv")
        if a and a.split("v")[0] in linked:
            now.add(a.split("v")[0])
            if i < census.FIRST_JUDGED_LINES:
                first.add(a.split("v")[0])
    print(f"linked, not in map: {len(linked)}; first crawl found {len(first)}; current crawl found {len(now)}")
    year = lambda a: "20" + a[:2]
    tot, f1, f2 = Counter(map(year, linked)), Counter(map(year, first)), Counter(map(year, now))
    for y in sorted(tot):
        print(f"  {y}: {tot[y]:4} linked, first {f1[y]:4}, now {f2[y]:4}")


if __name__ == "__main__":
    main()
