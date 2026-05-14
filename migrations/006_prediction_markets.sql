-- Migration 006: prediction_market_snapshots
--
-- Tracks probability snapshots for curated prediction market questions
-- from Kalshi (primary) and Manifold (fallback). Detects large moves
-- ("deltas") to surface breaking-news signals and populate the
-- "Market Signals" section of the daily digest.
--
-- Design:
--   - One row per (market_id, source). Updated in-place on every poll.
--   - A separate log table keeps the full history for delta calculations.
--   - The ingestor computes delta vs the previous snapshot before updating.

CREATE TABLE IF NOT EXISTS prediction_market_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id       TEXT NOT NULL,   -- platform-native ID (Kalshi ticker or Manifold slug)
    source          TEXT NOT NULL,   -- 'kalshi' | 'manifold'
    question        TEXT NOT NULL,   -- human-readable question
    probability     REAL NOT NULL,   -- current YES probability 0.0–1.0
    volume          REAL,            -- total volume / liquidity (USD or Mana)
    topic_tags      TEXT,            -- comma-separated topic labels (e.g. 'iran,hormuz')
    recorded_at     TEXT NOT NULL,   -- ISO8601 UTC
    UNIQUE (market_id, source)
);

CREATE TABLE IF NOT EXISTS prediction_market_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id       TEXT NOT NULL,
    source          TEXT NOT NULL,
    probability     REAL NOT NULL,
    delta_1h        REAL,            -- change vs snapshot 1h ago (NULL if no prior data)
    delta_24h       REAL,            -- change vs snapshot 24h ago
    recorded_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pmh_market_recorded
    ON prediction_market_history(market_id, source, recorded_at);
