-- Interpretability Map schema (SQLite; written to port cleanly to Postgres).
--
-- Core idea: claims are first-class and independent of documents. Documents
-- relate to claims through `claim_evidence` (proposes / assumes / supports /
-- refutes / replicates ...), and claims depend on other claims. Field-health
-- metrics are derived from those edges, never stored by hand.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id           INTEGER PRIMARY KEY,
    handle       TEXT NOT NULL UNIQUE,
    display_name TEXT
);

CREATE TABLE IF NOT EXISTS documents (
    id             INTEGER PRIMARY KEY,
    slug           TEXT NOT NULL UNIQUE,
    title          TEXT NOT NULL,
    doc_type       TEXT NOT NULL DEFAULT 'paper'
                   CHECK (doc_type IN ('paper','blog_post','essay','tweet_thread','talk','code','dataset','other')),
    source         TEXT NOT NULL DEFAULT 'web'
                   CHECK (source IN ('arxiv','lesswrong','alignmentforum','transformer_circuits','twitter','web','manual')),
    url            TEXT UNIQUE,
    arxiv_id       TEXT UNIQUE,
    external_ids   TEXT,             -- JSON: {"s2": ..., "doi": ..., "lw_post_id": ...}
    published_date TEXT,             -- ISO date
    abstract       TEXT,
    full_text      TEXT,
    summary        TEXT,             -- markdown: central idea, experiments, results, limitations
    -- stub: known only because something cites it; fetched: text pulled;
    -- extracted: summary/claims drafted; reviewed: a human has checked the extraction
    status         TEXT NOT NULL DEFAULT 'stub'
                   CHECK (status IN ('stub','fetched','extracted','reviewed')),
    provenance     TEXT,             -- who/what wrote the summary + extraction
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Other URLs for the same work (arXiv mirror of a transformer-circuits.pub page, etc.)
CREATE TABLE IF NOT EXISTS document_aliases (
    url         TEXT PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE
);

-- Citation-title variants retained after a document merge.
CREATE TABLE IF NOT EXISTS document_title_aliases (
    normalized_title TEXT PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS authors (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    affiliation TEXT
);

-- Alternate spellings / handles ("Christopher Olah" vs "Chris Olah", LessWrong usernames)
CREATE TABLE IF NOT EXISTS author_aliases (
    alias     TEXT PRIMARY KEY,
    author_id INTEGER NOT NULL REFERENCES authors(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS document_authors (
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    author_id   INTEGER NOT NULL REFERENCES authors(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    PRIMARY KEY (document_id, author_id)
);

CREATE TABLE IF NOT EXISTS concepts (
    id          INTEGER PRIMARY KEY,
    slug        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    description TEXT,
    kind        TEXT NOT NULL DEFAULT 'topic'
                CHECK (kind IN ('topic','method','phenomenon','hypothesis','tool','model_component','benchmark'))
);

-- A DAG, not a tree: a concept can have several foundations.
CREATE TABLE IF NOT EXISTS concept_relations (
    child_id  INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    parent_id INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    relation  TEXT NOT NULL DEFAULT 'subtopic_of'
              CHECK (relation IN ('subtopic_of','builds_on','alternative_to')),
    PRIMARY KEY (child_id, parent_id, relation)
);

CREATE TABLE IF NOT EXISTS document_concepts (
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    concept_id  INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    role        TEXT NOT NULL DEFAULT 'secondary' CHECK (role IN ('primary','secondary')),
    PRIMARY KEY (document_id, concept_id)
);

CREATE TABLE IF NOT EXISTS claims (
    id              INTEGER PRIMARY KEY,
    slug            TEXT NOT NULL UNIQUE,
    statement       TEXT NOT NULL,
    claim_type      TEXT NOT NULL DEFAULT 'empirical'
                    CHECK (claim_type IN ('empirical','theoretical','methodological','hypothesis')),
    scope           TEXT,           -- e.g. "small attention-only transformers", "Claude 3 Sonnet"
    -- optional human judgement that overrides the derived status
    status_override TEXT CHECK (status_override IN
                    ('untested','single_source','supported','replicated','contested','refuted')),
    status_note     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS claim_concepts (
    claim_id   INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    concept_id INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    PRIMARY KEY (claim_id, concept_id)
);

-- "claim X only makes sense if claim Y holds"
CREATE TABLE IF NOT EXISTS claim_dependencies (
    claim_id      INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    depends_on_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    note          TEXT,
    PRIMARY KEY (claim_id, depends_on_id)
);

-- How a document bears on a claim. One document can relate to many claims.
CREATE TABLE IF NOT EXISTS claim_evidence (
    id                 INTEGER PRIMARY KEY,
    claim_id           INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    document_id        INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    relation           TEXT NOT NULL CHECK (relation IN
                       ('proposes','assumes','supports','refutes','mixed',
                        'replicates','fails_to_replicate','extends','critiques')),
    strength           TEXT CHECK (strength IN ('weak','moderate','strong')),
    experiment_summary TEXT,        -- what was actually done to test it (if tested)
    models_studied     TEXT,        -- JSON list
    location           TEXT,        -- section / figure in the source
    provenance         TEXT,
    reviewed           INTEGER NOT NULL DEFAULT 0,
    UNIQUE (claim_id, document_id, relation)
);

-- Document lineage. Explicit citations and LLM-inferred influence are kept
-- apart so the graph never silently mixes fact with guesswork.
CREATE TABLE IF NOT EXISTS document_links (
    citing_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    cited_id   INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    link_type  TEXT NOT NULL CHECK (link_type IN ('explicit_citation','inferred_influence')),
    confidence REAL,                -- for inferred links
    rationale  TEXT,
    provenance TEXT,
    PRIMARY KEY (citing_id, cited_id, link_type)
);

-- Notes, ideas and critiques attached to any object. Multi-user from day one.
CREATE TABLE IF NOT EXISTS annotations (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    target_type TEXT NOT NULL CHECK (target_type IN ('document','claim','concept')),
    target_id   INTEGER NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'note'
                CHECK (kind IN ('note','idea','critique','question','next_step','dead_end')),
    body        TEXT NOT NULL,
    visibility  TEXT NOT NULL DEFAULT 'private' CHECK (visibility IN ('private','public')),
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------- automated updates

-- Things discovery found that might be worth ingesting. Triage scores relevance
-- before anything is fetched or extracted, so plan usage goes to the best ones.
CREATE TABLE IF NOT EXISTS candidates (
    id             INTEGER PRIMARY KEY,
    url            TEXT NOT NULL UNIQUE,
    title          TEXT NOT NULL,
    source         TEXT NOT NULL,      -- arxiv / lesswrong / transformer_circuits / citation
    published_date TEXT,
    authors        TEXT,               -- JSON list
    abstract       TEXT,
    signal         REAL,               -- karma (LW/AF) or citations from ingested docs
    reason         TEXT,               -- why discovery picked it up
    relevance      INTEGER,            -- 0-3 from triage; NULL = not yet triaged
    triage_note    TEXT,
    status         TEXT NOT NULL DEFAULT 'new'
                   CHECK (status IN ('new','accepted','rejected','ingested','failed')),
    document_id    INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    discovered_at  TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at     TEXT
);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS extraction_failures (
    id          INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    error       TEXT,
    at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS update_runs (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    summary     TEXT                   -- JSON
);

-- ---------------------------------------------------------------- human curation

-- A merged label keeps pointing at its survivor, so later extractions that use the old
-- slug attach to the right place. new_slug NULL means the label was deleted on purpose.
CREATE TABLE IF NOT EXISTS vocab_aliases (
    kind     TEXT NOT NULL CHECK (kind IN ('claim','concept')),
    old_slug TEXT NOT NULL,
    new_slug TEXT,
    PRIMARY KEY (kind, old_slug)
);

-- Pairs a reviewer has looked at and judged to be different ideas.
CREATE TABLE IF NOT EXISTS vocab_dismissed (
    kind TEXT NOT NULL,
    a    TEXT NOT NULL,
    b    TEXT NOT NULL,
    PRIMARY KEY (kind, a, b)
);

CREATE TABLE IF NOT EXISTS edit_log (
    id          INTEGER PRIMARY KEY,
    at          TEXT NOT NULL DEFAULT (datetime('now')),
    user_id     INTEGER REFERENCES users(id),
    action      TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_slug TEXT NOT NULL,
    detail      TEXT                  -- JSON
);

CREATE INDEX IF NOT EXISTS idx_evidence_claim ON claim_evidence(claim_id);
CREATE INDEX IF NOT EXISTS idx_evidence_doc   ON claim_evidence(document_id);
CREATE INDEX IF NOT EXISTS idx_links_cited    ON document_links(cited_id);
CREATE INDEX IF NOT EXISTS idx_annot_target   ON annotations(target_type, target_id);
