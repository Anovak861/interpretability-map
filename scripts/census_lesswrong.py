"""Page through every LessWrong / Alignment Forum post since 2016 and keep the AI-tagged ones.

    python scripts/census_lesswrong.py

Writes data/census/lesswrong_ai.jsonl (title, date, karma, AF flag, tag ids, first 1500 chars).
"""
from __future__ import annotations

import json
import time
from datetime import date, timedelta
from pathlib import Path

import httpx

OUT = Path(__file__).resolve().parent.parent / "data" / "census" / "lesswrong_ai.jsonl"
AI_TAGS = {"sYm3HiWcfZvrGu3ui": "AI", "56yXXrcxRjrQs6z9R": "Interpretability (ML & AI)"}
Q = """query ($before: String) { posts(input: {terms: {view: "new", limit: 500, after: "2016-01-01",
  before: $before}}) { results { _id title url postedAt baseScore af tags { _id name }
  contents { plaintextDescription } } } }"""


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    client = httpx.Client(timeout=120, headers={"User-Agent": "interp-map-census/0.1"})
    before, seen, kept = None, set(), 0
    with OUT.open("w", encoding="utf-8") as f:
        while True:
            for attempt in range(5):
                r = client.post("https://www.lesswrong.com/graphql", json={"query": Q, "variables": {"before": before}})
                if r.status_code == 200:
                    break
                time.sleep(10 * (attempt + 1))
            r.raise_for_status()
            posts = r.json()["data"]["posts"]["results"]
            fresh = [p for p in posts if p["_id"] not in seen]
            if not fresh:
                break
            for p in fresh:
                seen.add(p["_id"])
                tags = {t["_id"]: t["name"] for t in p.get("tags") or []}
                if not set(tags) & set(AI_TAGS):
                    continue
                kept += 1
                f.write(json.dumps({"id": p["_id"], "title": p["title"], "date": p["postedAt"][:10],
                                    "karma": p["baseScore"], "af": p.get("af"), "url": p.get("url"),
                                    "tags": list(tags.values()),
                                    "text": ((p.get("contents") or {}).get("plaintextDescription") or "")[:1500]}) + "\n")
            oldest = min(p["postedAt"] for p in posts)
            # newest-first: next page ends the day after the oldest post here, so posts from that
            # day that didn't fit are picked up (already-seen ones are skipped)
            before = (date.fromisoformat(oldest[:10]) + timedelta(days=1)).isoformat()
            print(f"  {len(seen)} posts scanned, {kept} AI-tagged, back to {oldest[:10]}", flush=True)
            time.sleep(1)
    print(f"done: {len(seen)} posts, {kept} AI-tagged")


if __name__ == "__main__":
    main()
