-- Dalila — initial SQLite schema
-- Single-file SQLite for the MVP. Migrate to Postgres + pgvector when scale demands.

CREATE TABLE IF NOT EXISTS sources (
    id              TEXT PRIMARY KEY,           -- matches sources.yaml id
    name            TEXT NOT NULL,
    kind            TEXT NOT NULL,              -- rss, gdelt, acled, scrape, idmc
    url             TEXT,
    quality         INTEGER NOT NULL DEFAULT 3, -- 1..5
    enabled         INTEGER NOT NULL DEFAULT 1, -- bool
    last_polled_at  TEXT,                       -- ISO8601 UTC
    last_error      TEXT,
    items_seen      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS items (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id             TEXT NOT NULL REFERENCES sources(id),
    url                   TEXT,                 -- canonical link
    url_hash              TEXT UNIQUE,          -- dedupe key (sha256 of normalised URL)
    title                 TEXT NOT NULL,
    body                  TEXT,
    author                TEXT,
    published_at          TEXT,                 -- ISO8601 UTC, may be null
    ingested_at           TEXT NOT NULL,        -- ISO8601 UTC

    -- Pre-filter result (cheap keyword/entity match before LLM)
    prefilter_passed      INTEGER NOT NULL DEFAULT 0,

    -- Classifier output (null until classified)
    classified_at         TEXT,
    category              TEXT,                 -- one of 8 categories or 'other'
    uae_relevance         REAL,                 -- 0..1
    severity              REAL,                 -- 0..1
    is_breaking_candidate INTEGER,              -- bool
    doctrine_relation     TEXT,                 -- reinforcing|refining|evolving|contradicting|new|null
    one_line_summary      TEXT,
    rationale             TEXT,
    entities_json         TEXT,                 -- JSON array of {name, in_watchlist}

    -- Dedup cluster id (simple URL+title-shingle approach for MVP)
    cluster_id            INTEGER,

    classifier_error      TEXT                  -- last error if classification failed
);

CREATE INDEX IF NOT EXISTS idx_items_ingested_at ON items(ingested_at);
CREATE INDEX IF NOT EXISTS idx_items_classified_at ON items(classified_at);
CREATE INDEX IF NOT EXISTS idx_items_category ON items(category);
CREATE INDEX IF NOT EXISTS idx_items_uae_relevance ON items(uae_relevance);
CREATE INDEX IF NOT EXISTS idx_items_cluster ON items(cluster_id);

-- Telegram subscribers
CREATE TABLE IF NOT EXISTS users (
    chat_id        INTEGER PRIMARY KEY,
    username       TEXT,
    first_name     TEXT,
    timezone       TEXT NOT NULL DEFAULT 'Asia/Dubai',
    digest_time    TEXT NOT NULL DEFAULT '06:30',
    enabled        INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL,
    last_digest_at TEXT
);

-- Composed digests
CREATE TABLE IF NOT EXISTS digests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    composed_at   TEXT NOT NULL,
    date_label    TEXT NOT NULL,                -- e.g. 'Friday 15 May 2026'
    content       TEXT NOT NULL,
    item_ids_json TEXT NOT NULL,                -- JSON array of item ids referenced
    item_count    INTEGER NOT NULL,
    feedback      TEXT
);

-- Per-user delivery log
CREATE TABLE IF NOT EXISTS deliveries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    digest_id    INTEGER NOT NULL REFERENCES digests(id),
    chat_id      INTEGER NOT NULL REFERENCES users(chat_id),
    delivered_at TEXT NOT NULL,
    success      INTEGER NOT NULL,
    error        TEXT
);

-- Daily call counter for the guardrail
CREATE TABLE IF NOT EXISTS llm_call_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    called_at   TEXT NOT NULL,                  -- ISO8601 UTC
    model       TEXT NOT NULL,                  -- haiku | sonnet | opus
    purpose     TEXT NOT NULL,                  -- classifier | editor | other
    duration_ms INTEGER,
    success     INTEGER NOT NULL,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_llm_called_at ON llm_call_log(called_at);
