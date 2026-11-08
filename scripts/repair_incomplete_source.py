"""Replace an incomplete-source extraction after a full Sol/high re-extraction and audit."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from interp_map import db, extract, pipeline, quality
from interp_map.models import Extraction


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("document_id", type=int)
    parser.add_argument("source_text", type=Path)
    parser.add_argument("--verification", type=Path)
    parser.add_argument("--reuse-draft", action="store_true")
    args = parser.parse_args()
    conn = db.connect()
    row = conn.execute("SELECT * FROM documents WHERE id=?", (args.document_id,)).fetchone()
    f = pipeline.fetched_from_db(conn, row)
    f.text = args.source_text.read_text(encoding="utf-8")
    vocab = pipeline.vocabulary_for(conn, row["id"], f)  # only the labels relevant to this document
    before_concepts = {r[0] for r in conn.execute("SELECT slug FROM concepts")}
    if args.reuse_draft:
        if f.text != row["full_text"]:
            raise ValueError("Draft reuse requires identical source text")
        draft = Extraction.model_validate_json((ROOT / "data/sustained-sol-high/drafts" / f"{args.document_id}.json").read_text(encoding="utf-8"))
        if draft.document.title != f.meta.title or db.normalize_url(draft.document.url) != db.normalize_url(f.meta.url):
            raise ValueError("Cached draft identity mismatch")
    else:
        draft = extract.via_codex_cli(f, vocab)
    verification = args.verification.read_text(encoding="utf-8") if args.verification else None
    audited = quality.audit_extraction(f, vocab, draft, source_verification=verification)
    out = ROOT / "data/source-repairs" / f"{args.document_id}-extraction.json"
    out.write_text(audited.model_dump_json(indent=2), encoding="utf-8")
    # Replace this document's evidence, including edges no longer in the corrected
    # extraction. Shared claim/concept definitions and other papers stay intact.
    try:
        conn.execute("DELETE FROM claim_evidence WHERE document_id=?", (args.document_id,))
        conn.execute("DELETE FROM document_concepts WHERE document_id=?", (args.document_id,))
        conn.execute("DELETE FROM document_links WHERE citing_id=? AND provenance LIKE 'codex-cli:%'", (args.document_id,))
        db.ingest_extraction(conn, audited, full_text=f.text)
    except Exception:
        conn.rollback()
        raise
    (extract.EXTRACTIONS_DIR / (row["slug"] + ".json")).write_text(audited.model_dump_json(indent=2), encoding="utf-8")
    (ROOT / "data/sustained-sol-high/drafts" / f"{args.document_id}.json").write_text(draft.model_dump_json(indent=2), encoding="utf-8")
    new_slugs = [r[0] for r in conn.execute("SELECT slug FROM concepts") if r[0] not in before_concepts]
    decisions = quality.merge_wave_concepts(conn, new_slugs)
    (ROOT / "data/source-repairs" / f"{args.document_id}-repair.json").write_text(json.dumps({
        "document_id": args.document_id, "old_chars": len(row["full_text"]),
        "new_chars": len(f.text), "claims": len(audited.claims), "concept_decisions": decisions,
        "provenance": audited.provenance}, indent=2), encoding="utf-8")
    print(f"Repaired {args.document_id}: {len(f.text)} characters, {len(audited.claims)} audited claims", flush=True)


if __name__ == "__main__":
    main()
