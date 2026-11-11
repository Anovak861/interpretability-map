"""Write the map's extracted data to data/export/*.jsonl, one file per table, for version control.

    python scripts/export_extracted.py

Leaves out what shouldn't be published: documents.full_text (the papers themselves), reviewer
accounts and notes, the discovery queue, and operational logs. Rows are sorted so that a new
export diffs cleanly against the last one. The database is opened read-only.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "export"
TABLES = ["documents", "document_aliases", "document_title_aliases", "document_authors", "document_links",
          "document_concepts", "authors", "author_aliases", "claims", "claim_evidence", "claim_dependencies",
          "claim_concepts", "claim_links", "concepts", "concept_relations", "vocab_aliases"]
OMIT = {"documents": {"full_text"}}


def main():
    conn = sqlite3.connect(f"file:{(ROOT / 'data' / 'interp_map.db').as_posix()}?mode=ro", uri=True)
    OUT.mkdir(parents=True, exist_ok=True)
    for table in TABLES:
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")') if r[1] not in OMIT.get(table, ())]
        names = ", ".join(f'"{c}"' for c in cols)
        rows = conn.execute(f'SELECT {names} FROM "{table}" ORDER BY {names}')
        n = 0
        with (OUT / f"{table}.jsonl").open("w", encoding="utf-8", newline="\n") as f:
            for row in rows:
                f.write(json.dumps(dict(zip(cols, row)), ensure_ascii=False) + "\n")
                n += 1
        print(f"{table}: {n}")


if __name__ == "__main__":
    main()
