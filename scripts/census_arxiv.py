"""Harvest arXiv metadata (title, abstract, categories, dates) for a set via OAI-PMH.

    python scripts/census_arxiv.py [--set cs] [--from 2016-01-01]

Writes data/census/arxiv_<set>.jsonl (one record per line) and resumes from the saved
resumption token if interrupted. Used to size the interpretability / alignment literature.
"""
from __future__ import annotations

import argparse
import json
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

OUT = Path(__file__).resolve().parent.parent / "data" / "census"
URL = "https://oaipmh.arxiv.org/oai"
NS = {"oai": "http://www.openarchives.org/OAI/2.0/", "a": "http://arxiv.org/OAI/arXiv/"}


def text(el, path):
    x = el.find(path, NS)
    return " ".join(x.text.split()) if x is not None and x.text else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default="cs")
    ap.add_argument("--from", dest="since", default="2016-01-01")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    out, state = OUT / f"arxiv_{args.set}.jsonl", OUT / f"arxiv_{args.set}.token"
    token = state.read_text().strip() if state.exists() else None
    client = httpx.Client(timeout=120, headers={"User-Agent": "interp-map-census/0.1"}, follow_redirects=True)
    n = 0
    while True:
        params = ({"verb": "ListRecords", "resumptionToken": token} if token else
                  {"verb": "ListRecords", "metadataPrefix": "arXiv", "set": args.set, "from": args.since})
        for attempt in range(12):
            try:
                r = client.get(URL, params=params)
            except httpx.TransportError as e:
                print(f"  {type(e).__name__}; retrying in 60s", flush=True)
                time.sleep(60)
                continue
            if r.status_code == 200:
                break
            wait = int(r.headers.get("Retry-After", "30") or 30)
            print(f"  HTTP {r.status_code}; waiting {wait}s", flush=True)
            time.sleep(wait)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        err = root.find("oai:error", NS)
        if err is not None and err.get("code") == "badResumptionToken":
            # the saved token expired: restart the list from the last record's datestamp
            # (records already written are skipped downstream by id)
            last = max(json.loads(l)["updated"] or json.loads(l)["created"] for l in out.open(encoding="utf-8"))
            print(f"  resumption token expired; restarting from {last}", flush=True)
            token, args.since = None, last
            continue
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
        token = tok.text if tok is not None and tok.text else None
        total = tok.get("completeListSize") if tok is not None else "?"
        if not token:
            state.unlink(missing_ok=True)
            print(f"done: {n} records this run", flush=True)
            return
        state.write_text(token)
        print(f"  {n} records this run (list size {total})", flush=True)
        time.sleep(3)


if __name__ == "__main__":
    main()
