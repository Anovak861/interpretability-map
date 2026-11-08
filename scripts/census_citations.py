"""Recursive citation crawl over OpenAlex, starting from the map's documents.

    python scripts/census_citations.py [--reserve 20]

Each round expands the works judged relevant (interp_map.census.classify) in both directions:
backward to references cited by at least two relevant works, forward to works citing a
relevant work (skipping hubs with more than HUB citations, whose citers are mostly generic).
OpenAlex's anonymous budget is ~1000 requests/day, so the crawl saves its state and resumes
where it stopped on the next run. With a key (env OPENALEX_API_KEY, or the file
.secrets/openalex_key in the project root) the budget is ~10,000/day.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from interp_map import census, db  # noqa: E402

OUT = ROOT / "data" / "census"
STATE, WORKS = OUT / "openalex_state.json", OUT / "openalex_works.jsonl"
API = "https://api.openalex.org/works"
FIELDS = "id,doi,title,publication_date,type,cited_by_count,referenced_works,abstract_inverted_index,locations"
HUB = 2000
client = httpx.Client(timeout=90, headers={"User-Agent": "interp-map-census/0.1"})
KEY_FILE = ROOT / ".secrets" / "openalex_key"
API_KEY = os.environ.get("OPENALEX_API_KEY") or (KEY_FILE.read_text().strip() if KEY_FILE.exists() else None)
remaining = 10000 if API_KEY else 1000


class OutOfBudget(Exception):
    pass


def get(params: dict, reserve: int) -> dict:
    global remaining
    if remaining <= reserve:
        raise OutOfBudget
    if API_KEY:
        params = {**params, "api_key": API_KEY}
    for attempt in range(8):
        try:
            r = client.get(API, params=params)
        except httpx.TransportError as e:   # dropped connections happen on long crawls
            print(f"  {type(e).__name__}; retrying in 30s", flush=True)
            time.sleep(30)
            continue
        remaining = int(r.headers.get("x-ratelimit-remaining", remaining) or remaining)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429 and "daily" in r.text.lower():
            raise OutOfBudget
        time.sleep(int(r.headers.get("Retry-After", "10") or 10))
    # no raise_for_status(): its message includes the URL, and with it the API key
    raise RuntimeError(f"OpenAlex HTTP {r.status_code}: {r.text[:200]}")


def abstract(inv) -> str:
    if not inv:
        return ""
    pos = sorted((p, w) for w, ps in inv.items() for p in ps)
    return " ".join(w for _, w in pos)


def arxiv_of(w) -> str | None:
    doi = (w.get("doi") or "").lower()
    if "10.48550/arxiv." in doi:
        return doi.split("10.48550/arxiv.")[1]
    for loc in w.get("locations") or []:
        u = (loc.get("landing_page_url") or "")
        if "arxiv.org/abs/" in u:
            return u.split("arxiv.org/abs/")[1].split("v")[0] if u.split("arxiv.org/abs/")[1][:4].isdigit() else None
    return None


def record(w, hop: int, how: str) -> dict:
    ab = abstract(w.get("abstract_inverted_index"))
    return {"id": w["id"].rsplit("/", 1)[-1], "title": w.get("title"), "date": w.get("publication_date"),
            "type": w.get("type"), "cited_by": w.get("cited_by_count"), "arxiv": arxiv_of(w),
            "abstract": ab[:3000], "refs": [r.rsplit("/", 1)[-1] for r in w.get("referenced_works") or []],
            "label": census.classify(w.get("title"), ab), "hop": hop, "how": how}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reserve", type=int, default=20)
    ap.add_argument("--max-hops", type=int, default=4)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    works: dict[str, dict] = {}
    if WORKS.exists():
        for line in WORKS.open(encoding="utf-8"):
            r = json.loads(line)
            works[r["id"]] = r
    st = json.loads(STATE.read_text()) if STATE.exists() else {"seeded": False, "hop": 0, "expanded_fwd": [], "expanded_back": []}
    out = WORKS.open("a", encoding="utf-8")

    def add(w, hop, how):
        wid = w["id"].rsplit("/", 1)[-1]
        if wid in works:
            return False
        r = record(w, hop, how)
        works[wid] = r
        out.write(json.dumps(r) + "\n")
        return True

    def save():
        out.flush()
        STATE.write_text(json.dumps(st))

    try:
        if not st["seeded"]:
            conn = db.connect()
            ids = [r[0] for r in conn.execute("SELECT arxiv_id FROM documents WHERE arxiv_id IS NOT NULL")]
            for i in range(0, len(ids), 50):
                dois = "|".join(f"https://doi.org/10.48550/arxiv.{a}" for a in ids[i:i + 50])
                j = get({"filter": f"doi:{dois}", "select": FIELDS, "per_page": 50}, args.reserve)
                for w in j["results"]:
                    add(w, 0, "seed")
            st["seeded"] = True
            save()
            print(f"seeded {len(works)} works", flush=True)

        while st["hop"] < args.max_hops:
            hop = st["hop"]
            relevant = [r for r in works.values() if r["label"] and r["hop"] <= hop]
            # backward: references cited by at least two relevant works
            refc = Counter(x for r in relevant for x in r["refs"])
            back = [x for x, n in refc.items() if n >= 2 and x not in works and x not in st["expanded_back"]]
            for i in range(0, len(back), 50):
                chunk = back[i:i + 50]
                j = get({"filter": "openalex:" + "|".join(chunk), "select": FIELDS, "per_page": 50}, args.reserve)
                for w in j["results"]:
                    add(w, hop + 1, "reference")
                st["expanded_back"] += chunk
                save()
            # forward: works citing relevant, non-hub works
            done = set(st["expanded_fwd"])
            fwd = sorted((r for r in relevant if r["id"] not in done and (r["cited_by"] or 0) <= HUB),
                         key=lambda r: -(r["cited_by"] or 0))
            for i in range(0, len(fwd), 40):
                chunk = [r["id"] for r in fwd[i:i + 40]]
                # anonymous requests can't use cursors; plain pages reach the first 10,000 results
                page = 1
                while page <= 50:
                    j = get({"filter": "cites:" + "|".join(chunk), "select": FIELDS, "per_page": 200,
                             "page": page}, args.reserve)
                    for w in j["results"]:
                        add(w, hop + 1, "citation")
                    if len(j["results"]) < 200:
                        break
                    page += 1
                st["expanded_fwd"] += chunk
                save()
                print(f"  hop {hop + 1}: {len(works)} works, {sum(1 for r in works.values() if r['label'])} relevant, "
                      f"{remaining} requests left", flush=True)
            st["hop"] = hop + 1
            save()
            print(f"finished hop {hop + 1}: {len(works)} works, {sum(1 for r in works.values() if r['label'])} relevant", flush=True)
    except OutOfBudget:
        save()
        print(f"stopped for today's OpenAlex budget: {len(works)} works, "
              f"{sum(1 for r in works.values() if r['label'])} relevant; rerun tomorrow to resume", flush=True)


if __name__ == "__main__":
    main()
