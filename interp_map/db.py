"""SQLite access and the single ingest path for extractions."""
from __future__ import annotations

import json
import os
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Optional

from .models import Extraction, ReferenceRef

ROOT = Path(__file__).resolve().parent.parent
# INTERP_MAP_DB points everything at another database file (e.g. a scratch copy for testing)
DEFAULT_DB = Path(os.environ.get("INTERP_MAP_DB") or ROOT / "data" / "interp_map.db")
SCHEMA = Path(__file__).with_name("schema.sql")


def connect(path: Path = DEFAULT_DB, *, check_same_thread: bool = True) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    _add_column(conn, "documents", "curated", "INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "claims", "level", "TEXT")   # paradigm / thesis / finding; NULL = not sorted yet
    conn.execute("INSERT OR IGNORE INTO users (handle, display_name) VALUES ('me', 'Me')")
    # concept_relations gained relation types; SQLite can't alter a CHECK, so rebuild the table once
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'concept_relations'").fetchone()[0]
    if "related_to" not in sql:
        conn.commit()
        conn.executescript("""
            PRAGMA foreign_keys = OFF;
            BEGIN;
            ALTER TABLE concept_relations RENAME TO concept_relations_old;
            CREATE TABLE concept_relations (
                child_id  INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
                parent_id INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
                relation  TEXT NOT NULL DEFAULT 'subtopic_of'
                          CHECK (relation IN ('subtopic_of','builds_on','alternative_to','uses','related_to')),
                PRIMARY KEY (child_id, parent_id, relation)
            );
            INSERT INTO concept_relations SELECT * FROM concept_relations_old;
            DROP TABLE concept_relations_old;
            COMMIT;
            PRAGMA foreign_keys = ON;
        """)
    conn.commit()


def _add_column(conn, table: str, column: str, decl: str) -> None:
    """Columns added after a database was first created (CREATE TABLE IF NOT EXISTS won't add them)."""
    if column not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def slugify(text: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "untitled"


def normalize_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    url = url.strip().split("#")[0]
    # sites that serve a directory index: ".../paper/index.html" and ".../paper" are the same page
    if re.match(r"https?://(www\.)?(transformer-circuits\.pub|distill\.pub)/", url):
        url = re.sub(r"/index\.html?$", "", url)
    url = url.rstrip("/")
    url = re.sub(r"^http://", "https://", url)
    url = url.replace("://www.", "://")
    # arXiv abs/pdf/html variants all collapse to /abs/<id>
    m = re.match(r"https://arxiv\.org/(?:abs|pdf|html)/([^/?]+?)(?:v\d+)?(?:\.pdf)?$", url)
    if m:
        return f"https://arxiv.org/abs/{m.group(1)}"
    # LessWrong / AF post URLs: keep only the post id
    m = re.match(r"https://(lesswrong\.com|alignmentforum\.org)/posts/([A-Za-z0-9]+)", url)
    if m:
        return f"https://{m.group(1)}/posts/{m.group(2)}"
    return url


def _norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]", "", title.lower())


def title_quality(title: str) -> int:
    """Stub titles come from link text or bare URLs; prefer real, longer titles."""
    if re.match(r"https?://", title):
        return 0
    return min(len(title), 200)


# ---------------------------------------------------------------- documents

def find_document(conn, *, arxiv_id=None, url=None, title=None) -> Optional[sqlite3.Row]:
    if arxiv_id:
        row = conn.execute("SELECT * FROM documents WHERE arxiv_id = ?", (arxiv_id,)).fetchone()
        if row:
            return row
    url = normalize_url(url)
    if url:
        row = conn.execute("SELECT * FROM documents WHERE url = ?", (url,)).fetchone() or conn.execute(
            "SELECT d.* FROM document_aliases a JOIN documents d ON d.id = a.document_id WHERE a.url = ?",
            (url,)).fetchone()
        if row:
            return row
    if title:
        target = _norm_title(title)
        row = conn.execute(
            "SELECT d.* FROM document_title_aliases a JOIN documents d ON d.id=a.document_id "
            "WHERE a.normalized_title=?", (target,)).fetchone()
        if row:
            return row
        for row in conn.execute("SELECT * FROM documents"):
            if _norm_title(row["title"]) == target:
                return row
    return None


def _unique_slug(conn, table: str, base: str) -> str:
    slug, n = base, 2
    while conn.execute(f"SELECT 1 FROM {table} WHERE slug = ?", (slug,)).fetchone():
        slug, n = f"{base}-{n}", n + 1
    return slug


def upsert_document(conn, *, title, status="stub", **fields) -> int:
    """Insert a document, or fill in an existing one (e.g. upgrade a stub)."""
    fields["url"] = normalize_url(fields.get("url"))
    existing = find_document(conn, arxiv_id=fields.get("arxiv_id"), url=fields.get("url"), title=title)
    order = ["stub", "fetched", "extracted", "reviewed"]
    if existing:
        upgrading = order.index(status) >= order.index(existing["status"])
        # a weaker source (e.g. a bare citation) only fills gaps; it never overwrites
        updates = {k: v for k, v in fields.items()
                   if v is not None and (upgrading or existing[k] is None)}
        if upgrading:
            updates["status"] = status
            # between two stubs, keep whichever citation gave the better title
            if status != "stub" or title_quality(title) > title_quality(existing["title"]):
                updates["title"] = title
            if existing["status"] == "stub" and "title" in updates:
                # stub slugs come from link text ("forthcoming paper"); re-derive from the new title
                base = slugify(title)
                if not existing["slug"].startswith(base):
                    updates["slug"] = _unique_slug(conn, "documents", base)
        if not updates:
            return existing["id"]
        sets = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(f"UPDATE documents SET {sets}, updated_at = datetime('now') WHERE id = ?",
                     (*updates.values(), existing["id"]))
        return existing["id"]
    slug = _unique_slug(conn, "documents", slugify(title))
    cols = {"slug": slug, "title": title, "status": status, **{k: v for k, v in fields.items() if v is not None}}
    cur = conn.execute(f"INSERT INTO documents ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                       tuple(cols.values()))
    return cur.lastrowid


def set_authors(conn, doc_id: int, names: list[str]) -> None:
    if not names:
        return
    conn.execute("DELETE FROM document_authors WHERE document_id = ?", (doc_id,))
    for pos, name in enumerate(names):
        name = name.strip()
        alias = conn.execute("SELECT author_id FROM author_aliases WHERE alias = ?", (name,)).fetchone()
        if alias:
            author_id = alias[0]
        else:
            conn.execute("INSERT OR IGNORE INTO authors (name) VALUES (?)", (name,))
            author_id = conn.execute("SELECT id FROM authors WHERE name = ?", (name,)).fetchone()[0]
        conn.execute("INSERT OR IGNORE INTO document_authors VALUES (?, ?, ?)", (doc_id, author_id, pos))


def merge_author(conn, from_name: str, into_name: str) -> None:
    """Treat two author names as one person (matters for the independence check)."""
    src = conn.execute("SELECT id FROM authors WHERE name = ?", (from_name,)).fetchone()
    dst = conn.execute("SELECT id FROM authors WHERE name = ?", (into_name,)).fetchone()
    if not src or not dst:
        raise KeyError(f"unknown author: {from_name if not src else into_name}")
    conn.execute("UPDATE OR IGNORE document_authors SET author_id = ? WHERE author_id = ?", (dst[0], src[0]))
    conn.execute("DELETE FROM document_authors WHERE author_id = ?", (src[0],))
    conn.execute("UPDATE author_aliases SET author_id = ? WHERE author_id = ?", (dst[0], src[0]))
    conn.execute("DELETE FROM authors WHERE id = ?", (src[0],))
    conn.execute("INSERT OR REPLACE INTO author_aliases VALUES (?, ?)", (from_name, dst[0]))
    conn.commit()


def add_reference(conn, citing_id: int, ref: ReferenceRef, provenance: str) -> int:
    """Link to a cited document, creating a stub if we haven't ingested it yet."""
    source = "arxiv" if ref.arxiv_id else "web"
    cited_id = upsert_document(conn, title=ref.title, url=ref.url, arxiv_id=ref.arxiv_id,
                               source=source, status="stub")
    if cited_id != citing_id:
        conn.execute(
            "INSERT OR REPLACE INTO document_links VALUES (?, ?, ?, ?, ?, ?)",
            (citing_id, cited_id, ref.link_type, ref.confidence, ref.rationale, provenance),
        )
    return cited_id


# ---------------------------------------------------------------- concepts & claims

def _id_by_slug(conn, table: str, slug: str) -> Optional[int]:
    row = conn.execute(f"SELECT id FROM {table} WHERE slug = ?", (slug,)).fetchone()
    return row[0] if row else None


def resolve_slug(conn, kind: str, slug: str) -> Optional[str]:
    """Follow merge aliases to the surviving label; None if the label was deleted on purpose."""
    seen = set()
    while slug not in seen:
        seen.add(slug)
        row = conn.execute("SELECT new_slug FROM vocab_aliases WHERE kind = ? AND old_slug = ?",
                           (kind, slug)).fetchone()
        if row is None:
            return slug
        if row[0] is None:
            return None
        slug = row[0]
    return slug


def upsert_concept(conn, slug, name, description=None, kind="topic") -> int:
    cid = _id_by_slug(conn, "concepts", slug)
    if cid:
        # keep the first real description; a placeholder (no description) is replaced outright
        conn.execute("""UPDATE concepts SET
                          name = CASE WHEN description IS NULL AND ? IS NOT NULL THEN ? ELSE name END,
                          kind = CASE WHEN description IS NULL AND ? IS NOT NULL THEN ? ELSE kind END,
                          description = COALESCE(description, ?) WHERE id = ?""",
                     (description, name, description, kind, description, cid))
        return cid
    return conn.execute("INSERT INTO concepts (slug, name, description, kind) VALUES (?, ?, ?, ?)",
                        (slug, name, description, kind)).lastrowid


def ensure_concept(conn, slug: str) -> int:
    """Concept referenced as a parent before it has been described."""
    return _id_by_slug(conn, "concepts", slug) or upsert_concept(conn, slug, slug.replace("-", " ").title())


def upsert_claim(conn, slug, statement, claim_type="empirical", scope=None) -> int:
    cid = _id_by_slug(conn, "claims", slug)
    if cid:
        conn.execute("""UPDATE claims SET
                          statement  = CASE WHEN statement LIKE '[placeholder]%' THEN ? ELSE statement END,
                          claim_type = CASE WHEN statement LIKE '[placeholder]%' THEN ? ELSE claim_type END,
                          scope = COALESCE(scope, ?) WHERE id = ?""", (statement, claim_type, scope, cid))
        return cid
    return conn.execute("INSERT INTO claims (slug, statement, claim_type, scope) VALUES (?, ?, ?, ?)",
                        (slug, statement, claim_type, scope)).lastrowid


def ensure_claim(conn, slug: str) -> int:
    return _id_by_slug(conn, "claims", slug) or upsert_claim(
        conn, slug, f"[placeholder] {slug.replace('-', ' ')}", "hypothesis")


# ---------------------------------------------------------------- ingest

class CuratedDocumentError(Exception):
    """Re-importing would overwrite a reviewer's edits to this document."""


def ingest_extraction(conn, ex: Extraction, *, full_text: Optional[str] = None,
                      external_ids: Optional[dict] = None, force: bool = False) -> int:
    d = ex.document
    existing = find_document(conn, arxiv_id=d.arxiv_id, url=d.url, title=d.title)
    if existing is not None and existing["curated"] and not force:
        raise CuratedDocumentError(f"{existing['slug']} has been edited by hand; re-import with force to overwrite")
    doc_id = upsert_document(
        conn, title=d.title, doc_type=d.doc_type, source=d.source, url=d.url, arxiv_id=d.arxiv_id,
        published_date=d.published_date, abstract=d.abstract, full_text=full_text,
        external_ids=json.dumps(external_ids) if external_ids else None,
        summary=ex.summary, provenance=ex.provenance, status="extracted",
    )
    set_authors(conn, doc_id, d.authors)
    concept = lambda slug: resolve_slug(conn, "concept", slug)  # noqa: E731
    claim = lambda slug: resolve_slug(conn, "claim", slug)  # noqa: E731

    for c in ex.concepts:
        slug = concept(c.slug)
        if slug is None:
            continue  # deleted by a reviewer
        cid = upsert_concept(conn, slug, c.name, c.description, c.kind)
        conn.execute("INSERT OR REPLACE INTO document_concepts VALUES (?, ?, ?)", (doc_id, cid, c.role))
        for parent in filter(None, map(concept, c.parents)):
            parent_id = ensure_concept(conn, parent)
            # Reject only the proposed edge, preserving the rest of the extraction.
            # UNION (not UNION ALL) terminates even if legacy data has a cycle.
            closes_cycle = conn.execute("""
                WITH RECURSIVE ancestors(id) AS (
                    SELECT ?
                    UNION
                    SELECT r.parent_id FROM concept_relations r
                    JOIN ancestors a ON r.child_id = a.id
                    WHERE r.relation = 'subtopic_of'
                )
                SELECT 1 FROM ancestors WHERE id = ? LIMIT 1
                """, (parent_id, cid)).fetchone()
            if closes_cycle:
                conn.execute("""INSERT INTO edit_log(action,target_type,target_slug,detail)
                                VALUES ('reject_concept_parent','concept',?,?)""",
                             (slug, json.dumps({"parent": parent, "document_id": doc_id,
                                                "provenance": ex.provenance,
                                                "reason": "proposed parent edge would create a cycle"})))
                continue
            conn.execute("INSERT OR IGNORE INTO concept_relations (child_id, parent_id) VALUES (?, ?)",
                         (cid, parent_id))

    filed = []   # new claims, linked to their parents once every claim in the extraction exists
    for cl in ex.claims:
        slug = claim(cl.slug)
        if slug is None:
            continue
        is_new = _id_by_slug(conn, "claims", slug) is None
        claim_id = upsert_claim(conn, slug, cl.statement, cl.claim_type, cl.scope)
        if is_new:
            conn.execute("UPDATE claims SET level = ? WHERE id = ?", (cl.level or "finding", claim_id))
            filed.append((claim_id, cl))
        for c in filter(None, map(concept, cl.concepts)):
            conn.execute("INSERT OR IGNORE INTO claim_concepts VALUES (?, ?)", (claim_id, ensure_concept(conn, c)))
        for dep in filter(None, map(claim, cl.depends_on)):
            if dep != slug:
                conn.execute("INSERT OR IGNORE INTO claim_dependencies (claim_id, depends_on_id) VALUES (?, ?)",
                             (claim_id, ensure_claim(conn, dep)))
        conn.execute(
            """INSERT OR REPLACE INTO claim_evidence
               (claim_id, document_id, relation, strength, experiment_summary, models_studied, location, provenance)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (claim_id, doc_id, cl.relation, cl.strength, cl.experiment_summary,
             json.dumps(cl.models_studied) if cl.models_studied else None, cl.location, ex.provenance),
        )

    for claim_id, cl in filed:
        parent = claim(cl.under) if cl.under and cl.stance and cl.level != "thesis" else None
        row = conn.execute("SELECT id, level FROM claims WHERE slug = ?", (parent,)).fetchone() if parent else None
        if row and row["id"] != claim_id and row["level"] in ("paradigm", "thesis"):
            conn.execute("INSERT OR REPLACE INTO claim_links VALUES (?, ?, ?, ?)", (claim_id, row["id"], cl.stance, ex.provenance))

    for ref in ex.references:
        add_reference(conn, doc_id, ref, ex.provenance)

    conn.commit()
    return doc_id


def merge(conn, kind: str, from_slug: str, into_slug: str) -> None:
    """Fold a duplicate document / claim / concept into another, re-pointing every edge."""
    table = {"document": "documents", "claim": "claims", "concept": "concepts"}[kind]
    src, dst = _id_by_slug(conn, table, from_slug), _id_by_slug(conn, table, into_slug)
    if not src or not dst:
        raise KeyError(f"unknown {kind} slug: {from_slug if not src else into_slug}")
    # (table, column) pairs that reference this kind of object
    refs = {
        "document": [("document_authors", "document_id"), ("document_concepts", "document_id"),
                     ("claim_evidence", "document_id"), ("document_links", "citing_id"),
                     ("document_links", "cited_id"), ("document_aliases", "document_id"),
                     ("document_title_aliases", "document_id")],
        "claim": [("claim_concepts", "claim_id"), ("claim_dependencies", "claim_id"),
                  ("claim_dependencies", "depends_on_id"), ("claim_evidence", "claim_id"),
                  ("claim_links", "finding_id"), ("claim_links", "parent_id")],
        "concept": [("concept_relations", "child_id"), ("concept_relations", "parent_id"),
                    ("document_concepts", "concept_id"), ("claim_concepts", "concept_id")],
    }[kind]
    for t, col in refs:
        # OR IGNORE: rows that would duplicate an existing edge on the target are dropped below
        conn.execute(f"UPDATE OR IGNORE {t} SET {col} = ? WHERE {col} = ?", (dst, src))
        conn.execute(f"DELETE FROM {t} WHERE {col} = ?", (src,))
    conn.execute("UPDATE annotations SET target_id = ? WHERE target_type = ? AND target_id = ?", (dst, kind, src))
    if kind == "document":
        title = conn.execute("SELECT title FROM documents WHERE id = ?", (src,)).fetchone()[0]
        url = conn.execute("SELECT url FROM documents WHERE id = ?", (src,)).fetchone()[0]
        conn.execute("UPDATE candidates SET document_id=? WHERE document_id=?", (dst, src))
        conn.execute("UPDATE extraction_failures SET document_id=? WHERE document_id=?", (dst, src))
        conn.execute("DELETE FROM documents WHERE id = ?", (src,))
        conn.execute("INSERT OR IGNORE INTO document_title_aliases VALUES (?, ?)", (_norm_title(title), dst))
        if url:
            conn.execute("INSERT OR IGNORE INTO document_aliases VALUES (?, ?)", (url, dst))
        conn.execute("DELETE FROM document_links WHERE citing_id = cited_id")
    else:
        conn.execute(f"DELETE FROM {table} WHERE id = ?", (src,))
        # future extractions using the old slug land on the survivor
        conn.execute("UPDATE vocab_aliases SET new_slug = ? WHERE kind = ? AND new_slug = ?", (into_slug, kind, from_slug))
        conn.execute("INSERT OR REPLACE INTO vocab_aliases VALUES (?, ?, ?)", (kind, from_slug, into_slug))
        conn.execute("DELETE FROM claim_dependencies WHERE claim_id = depends_on_id")
        conn.execute("DELETE FROM claim_links WHERE finding_id = parent_id")
        if kind == "claim":
            break_dependency_cycles(conn)  # merged dependencies can close a loop
        conn.execute("DELETE FROM concept_relations WHERE child_id = parent_id")
    conn.commit()


def possible_duplicates(conn, threshold: float = 0.85) -> list[tuple[float, sqlite3.Row, sqlite3.Row]]:
    """Fuzzy title matches between documents, for a human to confirm with `merge`."""
    from difflib import SequenceMatcher

    rows = [r for r in conn.execute("SELECT id, slug, title, status FROM documents") if title_quality(r["title"])]
    keys = [_norm_title(r["title"]) for r in rows]
    pairs = []
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            a, b = keys[i], keys[j]
            # cheap prefilter before the quadratic-time ratio
            if abs(len(a) - len(b)) > 0.4 * max(len(a), len(b)) or a[:6] != b[:6]:
                continue
            ratio = SequenceMatcher(None, a, b).ratio()
            if ratio >= threshold or (len(a) > 25 and (a in b or b in a)):
                pairs.append((ratio, rows[i], rows[j]))
    return sorted(pairs, key=lambda t: -t[0])


_STOPWORDS = set("""a an the of in on for to and or is are be by as with that this from into
their its it can than more most such via over under through which while when where do does""".split())


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    # crude stemming so "features" ~ "feature", "autoencoders" ~ "autoencoder"
    return {w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w for w in words if w not in _STOPWORDS}


def _prefix_filter_pairs(sets: list[set[str]], t: float) -> set[tuple[int, int]]:
    """Candidate pairs for a Jaccard >= t similarity join, without comparing every pair.

    Prefix filtering: order each set's tokens rarest-first; two sets with Jaccard >= t must
    share a token within their first |x| - ceil(t*|x|) + 1 tokens. Exact (no pair that
    qualifies is missed), and the rare-first order keeps the candidate lists short."""
    import math

    freq = defaultdict(int)
    for s in sets:
        for w in s:
            freq[w] += 1
    index = defaultdict(list)
    pairs = set()
    # length filter: process sets smallest first; an earlier (smaller) set y can only reach
    # Jaccard >= t with x if |y| >= t*|x|
    for i in sorted(range(len(sets)), key=lambda k: len(sets[k])):
        s = sets[i]
        if not s:
            continue
        min_len = t * len(s)
        ordered = sorted(s, key=lambda w: (freq[w], w))
        for w in ordered[:len(s) - math.ceil(t * len(s)) + 1]:
            for j in index[w]:
                if len(sets[j]) >= min_len:
                    pairs.add((min(i, j), max(i, j)))
            index[w].append(i)
    return pairs


_dupes_cache: dict = {}


def possible_vocab_duplicates(conn, concept_threshold: float = 0.6,
                              claim_threshold: float = 0.5) -> list[tuple[float, str, str, str]]:
    """Cached wrapper: the comparison takes seconds on thousands of claims, and the answer only
    changes when claims, concepts, edits or dismissals change."""
    key = (concept_threshold, claim_threshold, *conn.execute(
        """SELECT (SELECT COUNT(*) || ':' || COALESCE(MAX(id), 0) FROM claims),
                  (SELECT COUNT(*) || ':' || COALESCE(MAX(id), 0) FROM concepts),
                  (SELECT COALESCE(MAX(id), 0) FROM edit_log),
                  (SELECT COUNT(*) FROM vocab_dismissed)""").fetchone(),
           conn.execute("PRAGMA database_list").fetchone()[2])  # which database file this is
    if key not in _dupes_cache:
        _dupes_cache.clear()
        _dupes_cache[key] = _possible_vocab_duplicates(conn, concept_threshold, claim_threshold)
    return list(_dupes_cache[key])


def _possible_vocab_duplicates(conn, concept_threshold: float = 0.6,
                               claim_threshold: float = 0.5) -> list[tuple[float, str, str, str]]:
    """Concepts / claims that look like the same idea under two slugs (likely from parallel
    extractions that couldn't see each other's new labels). Returns (score, kind, slug_a, slug_b).
    Scores are word overlap (Jaccard); slugs are compared on their own too, since parallel
    extractions often coin near-identical slugs ("sae-feature-absorption" / "feature-absorption-in-saes")."""
    out = []
    dismissed = {tuple(r) for r in conn.execute("SELECT kind, a, b FROM vocab_dismissed")}
    for kind, sql, threshold in (
            ("concept", "SELECT slug, name || ' ' || COALESCE(description, '') AS text FROM concepts", concept_threshold),
            ("claim", "SELECT slug, statement AS text FROM claims", claim_threshold)):
        rows = [(r["slug"], _tokens(r["text"]), _tokens(r["slug"].replace("-", " "))) for r in conn.execute(sql)]
        pairs = _prefix_filter_pairs([r[1] for r in rows], threshold) | \
            _prefix_filter_pairs([r[2] if len(r[2]) > 1 else set() for r in rows], threshold)
        for i, j in pairs:
            (sa, ta, ka), (sb, tb, kb) = rows[i], rows[j]
            text = len(ta & tb) / len(ta | tb) if ta and tb else 0
            slug = len(ka & kb) / len(ka | kb) if len(ka) > 1 and len(kb) > 1 else 0
            score = max(text, slug)
            if score >= threshold and (kind, *sorted((sa, sb))) not in dismissed:
                out.append((score, kind, sa, sb))
    return sorted(out, reverse=True)


def vocabulary(conn) -> dict:
    """Existing concepts and claims, handed to the extractor so it reuses slugs."""
    return {
        "concepts": [dict(r) for r in conn.execute("SELECT slug, name, kind FROM concepts ORDER BY slug")],
        "claims": [dict(r) for r in conn.execute("SELECT slug, statement FROM claims ORDER BY slug")],
    }


def break_dependency_cycles(conn) -> int:
    """Remove the dependency links that close cycles, returning how many were removed.

    Merging claims re-points their dependencies, which can leave A depending on B depending
    on A; every claim upstream of such a cycle then appears to depend on all of it. Within
    each strongly connected component, a link "A depends on B" is kept only if B is the more
    foundational claim (more documents, then lower id), which makes the component acyclic.
    """
    edges = conn.execute("SELECT claim_id, depends_on_id FROM claim_dependencies").fetchall()
    graph = defaultdict(list)
    for a, b in edges:
        graph[a].append(b)
    # iterative Tarjan's algorithm (no recursion limit on long chains)
    index, low, on_stack, stack, comp_of, counter = {}, {}, set(), [], {}, 0
    for root in list(graph):
        if root in index:
            continue
        work = [(root, 0)]
        while work:
            v, i = work.pop()
            if i == 0:
                index[v] = low[v] = counter
                counter += 1
                stack.append(v)
                on_stack.add(v)
            children = graph.get(v, [])
            if i < len(children):
                work.append((v, i + 1))
                w = children[i]
                if w not in index:
                    work.append((w, 0))
                elif w in on_stack:
                    low[v] = min(low[v], index[w])
                continue
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                for w in comp:
                    comp_of[w] = v
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[v])
    docs = dict(conn.execute("SELECT claim_id, COUNT(DISTINCT document_id) FROM claim_evidence GROUP BY claim_id"))
    rank = lambda c: (docs.get(c, 0), -c)  # noqa: E731  higher = more foundational
    drop = [(a, b) for a, b in edges
            if comp_of.get(a) is not None and comp_of.get(a) == comp_of.get(b) and not rank(b) > rank(a)]
    conn.executemany("DELETE FROM claim_dependencies WHERE claim_id = ? AND depends_on_id = ?", drop)
    return len(drop)


def relevant_vocabulary(conn, doc_id: Optional[int], title: str, abstract: str = "", text: str = "", *,
                        max_claims: int = 260, max_concepts: int = 250, hub_claims: int = 80) -> dict:
    """The part of the vocabulary that matters for one document.

    Sending every label with every extraction (hundreds of thousands of tokens once the map
    is large) is expensive and hides the right claim in noise, so the extractor coins a new
    one instead. A document mostly tests, extends or assumes claims attached to the papers
    it cites, so those come first; then claims matching its title, abstract and text
    (rare words weighted, as in TF-IDF); then the most widely shared claims. Every concept
    slug is still listed, with names and descriptions for the most relevant ones.
    """
    import math
    from collections import Counter

    claims = {r["id"]: dict(r) for r in conn.execute("SELECT id, slug, statement, scope, level FROM claims")}
    if any(c["level"] in ("paradigm", "thesis") for c in claims.values()):
        # once the map is sorted, findings are not reuse targets: evidence lands on broad claims
        claims = {i: c for i, c in claims.items() if c["level"] in ("paradigm", "thesis")}
    shared = dict(conn.execute("SELECT claim_id, COUNT(DISTINCT document_id) FROM claim_evidence GROUP BY claim_id"))
    picked: dict[int, str] = {}

    def take(ids, why, limit):
        for i in ids:
            if len(picked) >= max_claims or limit <= 0:
                return
            if i in claims and i not in picked:
                picked[i] = why
                limit -= 1

    # 1. claims on the documents this one cites (and the claims those depend on)
    cited_claims = []
    if doc_id is not None:
        cited_claims = [r[0] for r in conn.execute(
            """SELECT e.claim_id FROM document_links l JOIN claim_evidence e ON e.document_id = l.cited_id
               WHERE l.citing_id = ? GROUP BY e.claim_id ORDER BY COUNT(*) DESC""", (doc_id,))]
    take(sorted(cited_claims, key=lambda i: -shared.get(i, 0)), "cited", max_claims // 2)

    # 2. lexical relevance to this document
    stop = _STOPWORDS | {"model", "models", "language", "can", "provide", "provides", "useful"}
    words = lambda s: [w for w in _tokens(s or "") if len(w) > 2 and w not in stop]  # noqa: E731
    title_tf, abstract_tf, body_tf = Counter(words(title)), Counter(words(abstract)), Counter(words(text[:200_000]))
    terms = {i: set(words(f"{c['statement']} {c['slug'].replace('-', ' ')}")) for i, c in claims.items()}
    df = Counter(t for ts in terms.values() for t in ts)
    n = len(claims) or 1

    def score(i):
        return sum(math.log(1 + n / (1 + df[t])) * (8 * title_tf[t] + 4 * abstract_tf[t] + math.log1p(body_tf[t]))
                   for t in terms[i]) / math.sqrt(len(terms[i]) or 1)
    ranked = sorted((i for i in claims if i not in picked), key=score, reverse=True)
    take([i for i in ranked if score(i) > 0], "lexical", max_claims - hub_claims - len(picked))

    # 3. the field's most widely shared claims
    take(sorted(claims, key=lambda i: -shared.get(i, 0))[:hub_claims * 2], "widely shared", hub_claims)

    # claims the picked ones depend on, so dependency slugs resolve
    deps = [r[0] for r in conn.execute(
        f"SELECT depends_on_id FROM claim_dependencies WHERE claim_id IN ({','.join('?' * len(picked)) or 'NULL'})",
        tuple(picked))]
    take(deps, "dependency", 40)

    # concepts: everything the picked claims and cited documents use, plus lexical matches
    concepts = {r["id"]: dict(r) for r in conn.execute("SELECT id, slug, name, kind, description FROM concepts")}
    near = Counter()
    for (cid,) in conn.execute(
            f"SELECT concept_id FROM claim_concepts WHERE claim_id IN ({','.join('?' * len(picked)) or 'NULL'})", tuple(picked)):
        near[cid] += 2
    if doc_id is not None:
        for (cid,) in conn.execute(
                "SELECT dc.concept_id FROM document_links l JOIN document_concepts dc ON dc.document_id = l.cited_id "
                "WHERE l.citing_id = ?", (doc_id,)):
            near[cid] += 3
    for cid, c in concepts.items():
        overlap = set(words(f"{c['name']} {c['slug'].replace('-', ' ')}")) & (set(title_tf) | set(abstract_tf))
        near[cid] += 4 * len(overlap)
    top = [cid for cid, _ in near.most_common(max_concepts)]
    return {
        "claims": [{"slug": claims[i]["slug"], "statement": claims[i]["statement"],
                    **({"level": claims[i]["level"]} if claims[i]["level"] else {})}
                   for i in sorted(picked, key=lambda i: claims[i]["slug"])],
        "concepts": [{"slug": concepts[c]["slug"], "name": concepts[c]["name"], "kind": concepts[c]["kind"]}
                     for c in sorted(top, key=lambda c: concepts[c]["slug"])],
        "other_concept_slugs": sorted(c["slug"] for cid, c in concepts.items() if cid not in set(top)),
        "note": (f"Showing the {len(picked)} existing paradigms and broad claims most relevant to this document (of {len(claims)}); "
                 "reuse them where they fit. other_concept_slugs lists every other existing concept."),
    }


def annotate(conn, target_type: str, target_slug: str, body: str, kind: str = "note",
             user: str = "me", visibility: str = "private") -> int:
    table = {"document": "documents", "claim": "claims", "concept": "concepts"}[target_type]
    target_id = _id_by_slug(conn, table, target_slug)
    if target_id is None:
        raise KeyError(f"no {target_type} with slug {target_slug!r}")
    user_id = conn.execute("SELECT id FROM users WHERE handle = ?", (user,)).fetchone()[0]
    aid = conn.execute(
        "INSERT INTO annotations (user_id, target_type, target_id, kind, body, visibility) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, target_type, target_id, kind, body, visibility),
    ).lastrowid
    conn.commit()
    return aid
