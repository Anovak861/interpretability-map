"""Count arXiv papers matching interpretability / alignment keyword queries, per half-year,
using the arXiv API's totalResults (one request per count, 3 s apart).

    python scripts/census_arxiv_counts.py   -> data/census/arxiv_counts.json
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import httpx

OUT = Path(__file__).resolve().parent.parent / "data" / "census" / "arxiv_counts.json"
LM = '(abs:"language model" OR abs:"language models" OR abs:LLM OR abs:LLMs OR abs:transformer OR abs:transformers)'
QUERIES = {
    "interp": '(abs:interpretability OR abs:mechanistic OR abs:"sparse autoencoder" OR abs:"sparse autoencoders" OR '
              'abs:probing OR abs:"activation patching" OR abs:"steering vector" OR abs:"steering vectors" OR '
              'abs:"logit lens" OR abs:superposition OR abs:circuits OR abs:"attention heads" OR abs:neurons OR '
              'abs:"representation engineering" OR abs:"internal representations" OR abs:explainability)',
    "align": '(abs:alignment OR abs:RLHF OR abs:jailbreak OR abs:jailbreaks OR abs:"reward hacking" OR abs:sycophancy OR '
             'abs:deception OR abs:deceptive OR abs:"red teaming" OR abs:refusal OR abs:"AI safety" OR abs:scheming OR '
             'abs:backdoor OR abs:"reward model" OR abs:"human feedback" OR abs:harmful OR abs:unlearning OR '
             'abs:"scalable oversight" OR abs:"misalignment" OR abs:honesty OR abs:truthfulness)',
}
PERIODS = [(f"{y}{m1}010000", f"{y}{m2}312359") for y in range(2016, 2027) for m1, m2 in (("01", "06"), ("07", "12"))]


def count(client, q):
    for attempt in range(6):
        r = client.get("https://export.arxiv.org/api/query", params={"search_query": q, "max_results": 0})
        if r.status_code == 200:
            m = re.search(r"<opensearch:totalResults[^>]*>(\d+)<", r.text)
            if m:
                return int(m.group(1))
        time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"no count for {q[:80]}")


def main():
    client = httpx.Client(timeout=120, headers={"User-Agent": "interp-map-census/0.1"})
    out = json.loads(OUT.read_text()) if OUT.exists() else {}
    for name, q in list(QUERIES.items()) + [("union", f"({QUERIES['interp']} OR {QUERIES['align']})")]:
        for a, b in PERIODS:
            key = f"{name}:{a[:6]}"
            if key in out:
                continue
            out[key] = count(client, f"{q} AND {LM} AND submittedDate:[{a} TO {b}]")
            OUT.write_text(json.dumps(out, indent=1))
            print(key, out[key], flush=True)
            time.sleep(3.1)


if __name__ == "__main__":
    main()
