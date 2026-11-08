"""Command-line entry point.

    python cli.py init
    python cli.py add <url> [--backend queue|claude_cli|codex_cli|api]
    python cli.py add-manual thread.txt --title "..." --url https://x.com/... --authors "A, B"
    python cli.py import [extraction.json ...]     (default: everything in data/extractions)
    python cli.py seed                             (fetch + queue every URL in seed/seed_list.json)
    python cli.py report
    python cli.py show <slug>
    python cli.py note <document|claim|concept> <slug> "text" [--kind idea|critique|next_step|...]
    python cli.py inbox
    python cli.py backfill-refs                    (retry Semantic Scholar citations)
    python cli.py resolve-stubs                    (real titles for cited arXiv works not yet ingested)
    python cli.py dupes                            (likely duplicate documents to merge)
    python cli.py merge <document|claim|concept> <from-slug> <into-slug>
    python cli.py merge author "Christopher Olah" "Chris Olah"

  Automated upkeep (see config.json):
    python cli.py update [--dry-run] [--skip-discovery] [--skip-triage]   discover -> triage -> fetch -> extract -> log
    python cli.py discover                         find new candidates (arXiv, LW/AF, Transformer Circuits, cited stubs)
    python cli.py triage                           score untriaged candidates (one headless call per batch)
    python cli.py candidates [--status new]        list candidates and their triage scores
    python cli.py accept <candidate-id> ...        force-ingest candidates on the next run
    python cli.py add-canon [seed/canon.json]      queue hand-picked foundational works (title-checked)
    python cli.py extract-queue [--workers 3] [--wave-size 6] [--limit N]
    python cli.py vocab-dupes                      concept / claim labels that look duplicated
    python cli.py regions                          large concepts that divide the field with little overlap
    python cli.py runs                             recent update runs
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from interp_map import db, extract, fetchers, pipeline, report
from interp_map.models import Extraction

ROOT = Path(__file__).resolve().parent


store_fetched = pipeline.store_fetched
fetched_from_db = pipeline.fetched_from_db


def run_extraction(conn, doc_id: int, f: fetchers.Fetched, backend: str) -> None:
    slug = conn.execute("SELECT slug FROM documents WHERE id = ?", (doc_id,)).fetchone()[0]
    vocab = pipeline.vocabulary_for(conn, doc_id, f)
    if backend == "queue":
        path = extract.queue(slug, f, vocab)
        print(f"  queued for extraction: {path.relative_to(ROOT)}")
        return
    ex = extract.extraction_backend(backend)(f, vocab)
    extract.EXTRACTIONS_DIR.mkdir(parents=True, exist_ok=True)
    (extract.EXTRACTIONS_DIR / f"{slug}.json").write_text(ex.model_dump_json(indent=2), encoding="utf-8")
    db.ingest_extraction(conn, ex)
    print(f"  extracted: {len(ex.concepts)} concepts, {len(ex.claims)} claims")


def cmd_add(args, conn):
    f = fetchers.fetch(args.url)
    doc_id = store_fetched(conn, f)
    print(f"fetched [{f.meta.source}] {f.meta.title}  ({len(f.text):,} chars, {len(f.references)} refs)")
    run_extraction(conn, doc_id, f, args.backend)


def cmd_add_manual(args, conn):
    f = fetchers.fetch_manual(Path(args.file), title=args.title, url=args.url,
                              authors=[a.strip() for a in args.authors.split(",") if a.strip()],
                              doc_type=args.type, date=args.date)
    doc_id = store_fetched(conn, f)
    print(f"added [{f.meta.source}] {f.meta.title}")
    run_extraction(conn, doc_id, f, args.backend)


def cmd_import(args, conn):
    paths = [Path(p) for p in args.files] or sorted(extract.EXTRACTIONS_DIR.glob("*.json"))
    for p in paths:
        ex = Extraction.model_validate_json(p.read_text(encoding="utf-8"))
        existing = db.find_document(conn, arxiv_id=ex.document.arxiv_id, url=ex.document.url,
                                    title=ex.document.title)
        if existing and existing["full_text"]:
            # fetched metadata is authoritative over the extraction's restatement of it
            ex.document = fetched_from_db(conn, existing).meta
        try:
            db.ingest_extraction(conn, ex, force=args.force)
        except db.CuratedDocumentError as e:
            print(f"skipped {p.name}: {e}")
            continue
        print(f"imported {p.name}: {len(ex.concepts)} concepts, {len(ex.claims)} claims, {len(ex.references)} refs")


def cmd_seed(args, conn):
    seed = json.loads((ROOT / "seed" / "seed_list.json").read_text(encoding="utf-8"))
    for item in seed:
        if item.get("manual"):
            continue
        try:
            existing = db.find_document(conn, url=item["url"])
            if existing and existing["status"] != "stub":
                print(f"skip (already {existing['status']}): {existing['title']}")
                continue
            f = fetchers.fetch(item["url"])
            doc_id = store_fetched(conn, f)
            print(f"fetched [{f.meta.source}] {f.meta.title}  ({len(f.text):,} chars, {len(f.references)} refs)")
            if not args.fetch_only:
                run_extraction(conn, doc_id, f, args.backend)
        except Exception as e:  # keep going; one bad source shouldn't stop the batch
            print(f"FAILED {item['url']}: {e}", file=sys.stderr)


STATUS_ORDER = ["replicated", "supported", "single_source", "untested", "contested", "refuted"]


def cmd_report(args, conn):
    claims = report.claim_health(conn)
    print("\n== Concepts (claims include sub-concepts) ==")
    print(f"{'concept':38} {'docs':>4} {'claims':>6} {'solid':>6} {'fragile':>7}  status mix")
    for r in report.concept_health(conn, claims):
        if not r["claims"] and not args.all:
            continue
        solid = f"{r['solid_share']:.0%}" if r["solid_share"] is not None else "-"
        mix = " ".join(f"{s}:{r['statuses'][s]}" for s in STATUS_ORDER if r["statuses"].get(s))
        print(f"{r['slug'][:38]:38} {r['documents']:>4} {r['claims']:>6} {solid:>6} {r['fragile']:>7}  {mix}")

    fragile = sorted((c for c in claims.values() if c.fragile_foundation), key=lambda c: -c.load)
    print("\n== Fragile foundations: relied on, but weakly tested ==")
    for c in fragile:
        print(f"[{c.status}] {c.slug}  (assumed by {c.assumed_by} docs, {c.depended_on_by} dependent claims)")
        print(f"    {c.statement}")

    print("\n== All claims ==")
    for c in sorted(claims.values(), key=lambda c: (STATUS_ORDER.index(c.status), -c.load)):
        print(f"[{c.status:13}] +{c.positive_docs}/-{c.negative_docs}/~{c.mixed_docs} groups={c.independent_groups} "
              f"load={c.load}  {c.slug}{'  (critiqued)' if c.critiqued_by else ''}")


def cmd_show(args, conn):
    for table in ("documents", "claims", "concepts"):
        row = conn.execute(f"SELECT * FROM {table} WHERE slug = ?", (args.slug,)).fetchone()
        if row:
            break
    else:
        sys.exit(f"nothing with slug {args.slug!r}")
    if table == "documents":
        authors = [r[0] for r in conn.execute(
            "SELECT a.name FROM document_authors da JOIN authors a ON a.id=da.author_id "
            "WHERE document_id=? ORDER BY position", (row["id"],))]
        print(f"{row['title']}\n{', '.join(authors)} · {row['published_date']} · {row['source']} · {row['status']}")
        print(row["url"] or "", "\n")
        print(row["summary"] or "(no summary yet)")
        print("\nClaims:")
        for r in conn.execute("SELECT c.slug, e.relation, e.strength FROM claim_evidence e "
                              "JOIN claims c ON c.id=e.claim_id WHERE e.document_id=?", (row["id"],)):
            print(f"  {r['relation']:18} {r['strength'] or '':8} {r['slug']}")
        def edge(r, citing_date, cited_date):
            # pages like transformer-circuits.pub get updated with links to later work
            later = citing_date and cited_date and cited_date > citing_date
            return f"  [{'later link (page update)' if later else r['link_type']}] {r['title']}"
        print("\nBuilds on:")
        for r in conn.execute("SELECT d.title, d.published_date, l.link_type FROM document_links l "
                              "JOIN documents d ON d.id=l.cited_id WHERE l.citing_id=? AND d.status != 'stub' "
                              "ORDER BY d.published_date", (row["id"],)):
            print(edge(r, row["published_date"], r["published_date"]))
        print("\nBuilt on by:")
        for r in conn.execute("SELECT d.title, d.published_date, l.link_type FROM document_links l "
                              "JOIN documents d ON d.id=l.citing_id WHERE l.cited_id=? ORDER BY d.published_date",
                              (row["id"],)):
            print(edge(r, r["published_date"], row["published_date"]))
    elif table == "claims":
        h = report.claim_health(conn)[row["id"]]
        print(f"{row['statement']}\nstatus: {h.status} (derived: {h.derived_status}) · load {h.load} · "
              f"concepts: {', '.join(h.concepts)}\n")
        for r in conn.execute("SELECT d.title, d.published_date, e.* FROM claim_evidence e "
                              "JOIN documents d ON d.id=e.document_id WHERE claim_id=? ORDER BY d.published_date",
                              (row["id"],)):
            print(f"  {r['relation']:18} {r['published_date'] or '':10} {r['title']}")
            if r["experiment_summary"]:
                print(f"      {r['experiment_summary']}")
        for label, sql in (("Depends on", "SELECT c.slug FROM claim_dependencies d JOIN claims c ON c.id=d.depends_on_id WHERE d.claim_id=?"),
                           ("Depended on by", "SELECT c.slug FROM claim_dependencies d JOIN claims c ON c.id=d.claim_id WHERE d.depends_on_id=?")):
            items = [r[0] for r in conn.execute(sql, (row["id"],))]
            if items:
                print(f"\n{label}: {', '.join(items)}")
    else:
        print(f"{row['name']} ({row['kind']})\n{row['description'] or ''}")
        parents = [r[0] for r in conn.execute("SELECT p.slug FROM concept_relations r JOIN concepts p ON p.id=r.parent_id WHERE r.child_id=?", (row["id"],))]
        kids = [r[0] for r in conn.execute("SELECT c.slug FROM concept_relations r JOIN concepts c ON c.id=r.child_id WHERE r.parent_id=?", (row["id"],))]
        print(f"parents: {', '.join(parents) or '-'}\nchildren: {', '.join(kids) or '-'}")
    tt = table[:-1]
    for r in conn.execute("SELECT a.kind, a.body, a.created_at, u.handle FROM annotations a JOIN users u ON u.id=a.user_id "
                          "WHERE target_type=? AND target_id=? ORDER BY a.created_at", (tt, row["id"])):
        print(f"\n[{r['kind']}] {r['handle']} {r['created_at']}: {r['body']}")


def cmd_backfill_refs(args, conn):
    """Retry Semantic Scholar citations for arXiv documents that have none yet."""
    rows = conn.execute("""SELECT id, arxiv_id, title FROM documents d WHERE arxiv_id IS NOT NULL
                           AND status != 'stub' AND NOT EXISTS (SELECT 1 FROM document_links l
                           WHERE l.citing_id = d.id AND l.link_type = 'explicit_citation')""").fetchall()
    for r in rows:
        refs, _ = fetchers.semantic_scholar_references(r["arxiv_id"])
        for ref in refs:
            db.add_reference(conn, r["id"], ref, "fetch:semantic_scholar")
        conn.commit()
        print(f"{len(refs):>4} refs  {r['title']}")


def cmd_merge(args, conn):
    from interp_map import curation
    if args.kind == "author":
        db.merge_author(conn, args.from_slug, args.into_slug)
    elif args.kind == "document":
        db.merge(conn, args.kind, args.from_slug, args.into_slug)
    else:
        curation.merge(conn, args.kind, args.from_slug, args.into_slug)  # records alias + edit history
    print(f"merged {args.kind} {args.from_slug} -> {args.into_slug}")


def cmd_dupes(args, conn):
    for ratio, a, b in db.possible_duplicates(conn):
        keep, drop = (a, b) if a["status"] != "stub" or b["status"] == "stub" else (b, a)
        print(f"{ratio:.2f}  {drop['title'][:70]}  ->  {keep['title'][:70]}")
        print(f"      python cli.py merge document {drop['slug']} {keep['slug']}")


def cmd_update(args, conn):
    cfg = pipeline.load_config()
    # one-off overrides for bigger or smaller runs than config.json describes
    for key, val in (("max_new_documents_per_run", args.max), ("workers", args.workers), ("wave_size", args.wave_size)):
        if val is not None:
            cfg["update"][key] = val
    if args.backend:
        cfg["update"]["backend"] = args.backend
    summary = pipeline.update(conn, cfg, skip_discovery=args.skip_discovery,
                              skip_triage=args.skip_triage, dry_run=args.dry_run)
    if summary.get("stopped"):
        print(f"\nstopped early: {summary['stopped']}")
    if not args.dry_run:
        print(f"\nlog written to {pipeline.LOG_DIR}")
    if "NotLoggedInError" in (summary.get("stopped") or ""):
        # a setup problem, unlike hitting the usage limit; let Task Scheduler show it as failed
        sys.exit(2)


def cmd_discover(args, conn):
    pipeline.discover(conn, pipeline.load_config())


def cmd_triage(args, conn):
    cfg = pipeline.load_config()
    if args.backend:
        cfg["triage"]["backend"] = args.backend
    print(f"scored {pipeline.triage(conn, cfg)} candidates")


def cmd_candidates(args, conn):
    sql = "SELECT * FROM candidates" + (" WHERE status = ?" if args.status else "")
    rows = conn.execute(sql, (args.status,) if args.status else ()).fetchall()
    rows = sorted(rows, key=lambda r: pipeline.priority(r) if r["relevance"] is not None else -1, reverse=True)
    for r in rows[:args.limit]:
        rel = "-" if r["relevance"] is None else r["relevance"]
        print(f"{r['id']:>5} [{rel}] {r['status']:9} {r['source']:20} {r['title'][:70]}")
        if r["triage_note"]:
            print(f"{'':24}{r['triage_note']}")


def cmd_add_canon(args, conn):
    items = json.loads(Path(args.file).read_text(encoding="utf-8"))
    print(f"added {pipeline.add_canon(conn, items)} canon candidates (accepted; titles verified at fetch)")


def cmd_accept(args, conn):
    # relevance 3 + 'accepted' puts them at the front of the next run's selection
    conn.executemany("UPDATE candidates SET status = 'accepted', relevance = 3, "
                     "triage_note = COALESCE(triage_note, 'accepted manually') WHERE id = ?",
                     [(i,) for i in args.ids])
    conn.commit()
    print(f"accepted {len(args.ids)} candidates")


def cmd_extract_queue(args, conn):
    stats = pipeline.extract_pending(conn, workers=args.workers, wave_size=args.wave_size,
                                     limit=args.limit, backend=args.backend,
                                     document_ids=args.document_ids)
    print(stats)


def cmd_vocab_dupes(args, conn):
    pairs = db.possible_vocab_duplicates(conn)
    if not pairs:
        print("no likely duplicate labels")
    for score, kind, a, b in pairs:
        print(f"{score:.2f} {kind:8} {a}  ~  {b}")
        print(f"      python cli.py merge {kind} {b} {a}    (or the other way round)")


def cmd_runs(args, conn):
    for r in conn.execute("SELECT * FROM update_runs ORDER BY id DESC LIMIT ?", (args.limit,)):
        s = json.loads(r["summary"])
        ex = s.get("extraction") or {}
        print(f"{r['started_at']}  selected {len(s.get('selected', []))}, extracted {ex.get('extracted', 0)}, "
              f"failed {ex.get('failed', 0)}, status changes {len(s.get('status_changes', []))}"
              f"{'  STOPPED: ' + s['stopped'][:60] if s.get('stopped') else ''}")


def cmd_regions(args, conn):
    """Which large concepts divide the field with little overlap (used to lay out the graphs)."""
    from interp_map import regions
    r = regions.choose_regions(conn, max_regions=args.max)
    print(f"{len(r['regions'])} regions cover {r['coverage']:.0%} of {r['documents']} documents\n")
    print(f"{'region':34} {'docs':>5} {'home':>5} {'concepts':>8}")
    for g in r["regions"]:
        print(f"{g.slug[:34]:34} {len(g.documents):>5} {g.exclusive:>5} {len(g.concepts):>8}")
    if len(r["regions"]) > 1:
        print("\ndocument overlap between regions (Jaccard):")
        width = max(len(g.slug[:20]) for g in r["regions"])
        for g, row in zip(r["regions"], r["overlap"]):
            print(f"  {g.slug[:20]:{width}}  " + " ".join(f"{x:4.2f}" for x in row))
    unassigned = r["documents"] - len(r["document_region"])
    if unassigned:
        print(f"\n{unassigned} documents fall in no region (shown as 'Other' in the lineage graph)")


def cmd_resolve_stubs(args, conn):
    print(f"resolved {pipeline.resolve_stub_titles(conn)} stub titles from arXiv")


def cmd_consolidate(args, conn):
    """Plan folding narrow claims into broader ones; nothing changes until reviewed and applied."""
    from interp_map import consolidate
    backend = args.backend or pipeline.load_config()["update"]["backend"]
    path = consolidate.plan(conn, backend=backend, limit_batches=args.batches, workers=args.workers, resume=args.resume)
    print(f"plan written to {path}  (review and apply it on the Review page)")


def cmd_fill_gaps(args, conn):
    """Find and queue documents likely to test the claims the field relies on most but the map
    has little evidence for. Run `update` afterwards to fetch and extract them."""
    from interp_map import gaps
    backend = args.backend or pipeline.load_config()["update"]["backend"]
    targets = gaps.evidence_gaps(conn, limit=args.claims, offset=args.offset)
    print(f"{len(targets)} evidence-gap claims; asking for sources that test them")
    works = gaps.suggest_sources(conn, targets, backend=backend)
    print(gaps.queue_verified(conn, works))


def cmd_recheck_gaps(args, conn):
    """Ask whether works already in the map test the gap claims they were suggested for."""
    from interp_map import gaps
    backend = args.backend or pipeline.load_config()["update"]["backend"]
    if args.leads_from:
        slugs = list(json.loads(Path(args.leads_from).read_text(encoding="utf-8")))
        print(f"{len(gaps.mapped_leads(conn, slugs, backend=backend))} mapped documents to re-check")
    print(gaps.recheck(conn, backend=backend, workers=args.workers))


def cmd_note(args, conn):
    db.annotate(conn, args.target_type, args.slug, args.body, kind=args.kind)
    print("noted.")


def cmd_inbox(args, conn):
    print("== Waiting for extraction ==")
    for r in conn.execute("SELECT slug, title FROM documents WHERE status = 'fetched' ORDER BY created_at"):
        print(f"  {r['slug']}: {r['title']}")
    print("\n== Most-cited documents not yet ingested (stubs) ==")
    for r in conn.execute(
            "SELECT d.title, d.url, COUNT(*) AS n FROM document_links l JOIN documents d ON d.id = l.cited_id "
            "WHERE d.status = 'stub' GROUP BY d.id ORDER BY n DESC, d.title LIMIT ?", (args.limit,)):
        print(f"  {r['n']:>3}x  {r['title'][:90]}  {r['url'] or ''}")


def main():
    p = argparse.ArgumentParser(description="Interpretability Map")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    backend = dict(choices=["queue", "claude_cli", "codex_cli", "api"], default="queue")
    a = sub.add_parser("add"); a.add_argument("url"); a.add_argument("--backend", **backend)
    m = sub.add_parser("add-manual")
    m.add_argument("file"); m.add_argument("--title", required=True); m.add_argument("--url")
    m.add_argument("--authors", default=""); m.add_argument("--date")
    m.add_argument("--type", default="tweet_thread"); m.add_argument("--backend", **backend)
    i = sub.add_parser("import"); i.add_argument("files", nargs="*")
    i.add_argument("--force", action="store_true", help="overwrite documents that were edited by hand")
    s = sub.add_parser("seed"); s.add_argument("--backend", **backend); s.add_argument("--fetch-only", action="store_true")
    r = sub.add_parser("report"); r.add_argument("--all", action="store_true")
    sh = sub.add_parser("show"); sh.add_argument("slug")
    n = sub.add_parser("note"); n.add_argument("target_type", choices=["document", "claim", "concept"])
    n.add_argument("slug"); n.add_argument("body")
    n.add_argument("--kind", default="note", choices=["note", "idea", "critique", "question", "next_step", "dead_end"])
    sub.add_parser("backfill-refs")
    sub.add_parser("resolve-stubs")
    fg = sub.add_parser("fill-gaps", help="queue documents that test weakly evidenced, widely relied-on claims")
    fg.add_argument("--claims", type=int, default=60)
    fg.add_argument("--offset", type=int, default=0, help="skip the N most relied-on gap claims")
    fg.add_argument("--backend", choices=["claude_cli", "codex_cli"])
    rg = sub.add_parser("recheck-gaps", help="link already-mapped works to the gap claims they test")
    rg.add_argument("--leads-from", help="JSON file whose keys are claim slugs; rebuilds the lead list")
    rg.add_argument("--workers", type=int, default=3)
    rg.add_argument("--backend", choices=["claude_cli", "codex_cli"])
    cs = sub.add_parser("consolidate", help="plan merging duplicate and overly narrow claims")
    cs.add_argument("--batches", type=int, help="only the first N batches (largest first)")
    cs.add_argument("--backend", choices=["claude_cli", "codex_cli"])
    cs.add_argument("--workers", type=int, default=4)
    cs.add_argument("--resume", action="store_true", help="continue the latest partial plan")
    rg = sub.add_parser("regions"); rg.add_argument("--max", type=int, default=9)
    sub.add_parser("dupes")
    mg = sub.add_parser("merge"); mg.add_argument("kind", choices=["document", "claim", "concept", "author"])
    mg.add_argument("from_slug"); mg.add_argument("into_slug")
    up = sub.add_parser("update"); up.add_argument("--dry-run", action="store_true")
    up.add_argument("--skip-discovery", action="store_true")
    up.add_argument("--skip-triage", action="store_true", help="only ingest already-accepted or triaged candidates")
    up.add_argument("--max", type=int, help="documents to add this run (default from config.json)")
    up.add_argument("--workers", type=int); up.add_argument("--wave-size", type=int)
    up.add_argument("--backend", choices=["claude_cli", "codex_cli", "api"])
    sub.add_parser("discover")
    tr = sub.add_parser("triage"); tr.add_argument("--backend", choices=["claude_cli", "codex_cli"])
    ca = sub.add_parser("candidates"); ca.add_argument("--status", choices=["new", "accepted", "rejected", "ingested", "failed"])
    ca.add_argument("--limit", type=int, default=50)
    ac = sub.add_parser("accept"); ac.add_argument("ids", type=int, nargs="+")
    cn = sub.add_parser("add-canon"); cn.add_argument("file", nargs="?", default=str(ROOT / "seed" / "canon.json"))
    eq = sub.add_parser("extract-queue"); eq.add_argument("--workers", type=int, default=3)
    eq.add_argument("--wave-size", type=int, default=6); eq.add_argument("--limit", type=int)
    eq.add_argument("--backend", choices=["claude_cli", "codex_cli", "api"], default="claude_cli")
    eq.add_argument("--document-ids", type=int, nargs="+", help="extract only these IDs, in priority order")
    sub.add_parser("vocab-dupes")
    rn = sub.add_parser("runs"); rn.add_argument("--limit", type=int, default=10)
    ib = sub.add_parser("inbox"); ib.add_argument("--limit", type=int, default=25)
    args = p.parse_args()

    conn = db.connect()
    db.init(conn)
    handlers = {"init": lambda a, c: print(f"database ready at {db.DEFAULT_DB}"),
                "add": cmd_add, "add-manual": cmd_add_manual, "import": cmd_import, "seed": cmd_seed,
                "report": cmd_report, "backfill-refs": cmd_backfill_refs, "resolve-stubs": cmd_resolve_stubs, "fill-gaps": cmd_fill_gaps, "recheck-gaps": cmd_recheck_gaps, "consolidate": cmd_consolidate, "merge": cmd_merge, "regions": cmd_regions, "dupes": cmd_dupes, "show": cmd_show, "note": cmd_note, "inbox": cmd_inbox,
                "update": cmd_update, "discover": cmd_discover, "triage": cmd_triage, "candidates": cmd_candidates,
                "accept": cmd_accept, "add-canon": cmd_add_canon, "extract-queue": cmd_extract_queue, "vocab-dupes": cmd_vocab_dupes,
                "runs": cmd_runs}
    handlers[args.cmd](args, conn)


if __name__ == "__main__":
    main()
