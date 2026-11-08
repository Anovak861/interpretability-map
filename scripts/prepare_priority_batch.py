"""Fetch the reviewed mechanistic-interpretability reading list, with duplicate checks.

Run from the project root. Extraction is separate so each wave can be curated first.
"""
import json
import sys
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from interp_map import db, fetchers, pipeline


def existing_match(conn, title, url, arxiv_id=None):
    exact = db.find_document(conn, title=title, url=url, arxiv_id=arxiv_id)
    if exact:
        return exact
    normalized = db._norm_title(title)
    for row in conn.execute("SELECT * FROM documents"):
        candidate = db._norm_title(row["title"])
        if len(candidate) > 35 and SequenceMatcher(None, normalized, candidate).ratio() > .94:
            return row
    return None


def main():
    conn = db.connect()
    results = []
    for item in json.loads((db.ROOT / "seed/mechanistic_priority.json").read_text(encoding="utf-8")):
        try:
            existing = existing_match(conn, item["title"], item["url"], fetchers.arxiv_id_or_none(item["url"]))
            if existing and existing["status"] != "stub":
                results.append({**item, "id": existing["id"], "action": "reused", "status": existing["status"]})
                print(f"REUSE {existing['id']} {existing['title']}", flush=True)
                continue
            f = fetchers.fetch(item["url"])
            if not pipeline.title_matches([item["title"]], f.meta.title):
                raise ValueError(f"Title mismatch: {f.meta.title}")
            if len(f.text) < 4000 or f.text == f.meta.abstract:
                raise ValueError("Full text unavailable or suspiciously short; do not extract abstract only")
            actual = existing_match(conn, f.meta.title, f.meta.url, f.meta.arxiv_id)
            existing = actual or existing
            if existing:
                if existing["status"] != "stub":
                    results.append({**item, "id": existing["id"], "action": "reused", "status": existing["status"]})
                    continue
                # Attach verified identifiers to a title-matched stub, so store_fetched upgrades it.
                if f.meta.url:
                    conn.execute("INSERT OR IGNORE INTO document_aliases VALUES (?,?)",
                                 (db.normalize_url(f.meta.url), existing["id"]))
                conn.commit()
            doc_id = pipeline.store_fetched(conn, f)
            results.append({**item, "id": doc_id, "action": "upgraded" if existing else "added",
                            "status": "fetched", "text_chars": len(f.text)})
            print(f"FETCHED {doc_id} {f.meta.title} ({len(f.text)} chars)", flush=True)
        except Exception as error:
            results.append({**item, "error": str(error)})
            print(f"FAILED {item['title']}: {error}", flush=True)
        (db.ROOT / "data/codex-priority-fetch.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    conn.close()


if __name__ == "__main__":
    main()
