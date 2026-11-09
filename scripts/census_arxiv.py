"""Harvest arXiv metadata (title, abstract, categories, dates) for a set via OAI-PMH.

    python scripts/census_arxiv.py [--set cs] [--from 2016-01-01] [--days 7]

Writes data/census/arxiv_<set>.jsonl (one record per line). Requests bounded date windows,
because the server times out on open-ended lists covering years. Progress is saved after each
window in arxiv_<set>.window, so a rerun continues from there and --from is only used the first
time. A window that keeps failing is split in half. A window retried after a partial failure can
write duplicates; readers keep the last record per id.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

OUT = Path(__file__).resolve().parent.parent / "data" / "census"
URL = "https://oaipmh.arxiv.org/oai"
NS = {"oai": "http://www.openarchives.org/OAI/2.0/", "a": "http://arxiv.org/OAI/arXiv/"}


class Stalled(Exception):
    pass


def text(el, path):
    x = el.find(path, NS)
    return " ".join(x.text.split()) if x is not None and x.text else None


def request(client, params, tries):
    for _ in range(tries):
        try:
            r = client.get(URL, params=params)
        except httpx.TransportError as e:
            print(f"  {type(e).__name__}; retrying in 60s", flush=True)
            time.sleep(60)
            continue
        if r.status_code == 200:
            return ET.fromstring(r.content)
        wait = int(r.headers.get("Retry-After", "30") or 30)
        print(f"  HTTP {r.status_code}; waiting {wait}s", flush=True)
        time.sleep(wait)
    raise Stalled


def harvest_window(client, out, oai_set, start, end) -> int:
    params = {"verb": "ListRecords", "metadataPrefix": "arXiv", "set": oai_set,
              "from": start.isoformat(), "until": end.isoformat()}
    tries, n = 2, 0   # the opening request is the expensive one: give up fast and split instead
    while True:
        root = request(client, params, tries)
        err = root.find("oai:error", NS)
        if err is not None:
            if err.get("code") == "noRecordsMatch":
                return n
            raise Stalled
        with out.open("a", encoding="utf-8") as f:
            for rec in root.iterfind(".//oai:record", NS):
                m = rec.find(".//a:arXiv", NS)
                if m is None:
                    continue
                f.write(json.dumps({"id": text(m, "a:id"), "title": text(m, "a:title"),
                                    "abstract": text(m, "a:abstract"), "categories": text(m, "a:categories"),
                                    "created": text(m, "a:created"), "updated": text(m, "a:updated")}) + "\n")
                n += 1
        tok = root.find(".//oai:resumptionToken", NS)
        if tok is None or not tok.text:
            return n
        params, tries = {"verb": "ListRecords", "resumptionToken": tok.text}, 8
        time.sleep(3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default="cs")
    ap.add_argument("--from", dest="since", default="2016-01-01")
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    out, state = OUT / f"arxiv_{args.set}.jsonl", OUT / f"arxiv_{args.set}.window"
    client = httpx.Client(timeout=120, headers={"User-Agent": "interp-map-census/0.1"}, follow_redirects=True)
    day = dt.date.fromisoformat(state.read_text().strip() if state.exists() else args.since)
    today, span, total = dt.date.today(), args.days, 0
    while day <= today:
        end = min(day + dt.timedelta(days=span - 1), today)
        try:
            n = harvest_window(client, out, args.set, day, end)
        except Stalled:
            if span == 1:
                print(f"stalled on {day}; rerun to continue from there", flush=True)
                sys.exit(1)
            span = max(1, span // 2)
            print(f"  {day}: window failed, trying {span} days", flush=True)
            continue
        total += n
        print(f"  {day} to {end}: {n} records ({total} this run)", flush=True)
        day = end + dt.timedelta(days=1)
        state.write_text(day.isoformat())
        span = min(args.days, span * 2)
        time.sleep(3)
    print(f"done: {total} records this run", flush=True)


if __name__ == "__main__":
    main()
