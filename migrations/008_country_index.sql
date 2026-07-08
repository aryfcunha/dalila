-- Migration 008: small covering index for the country-page window scan.
--
-- The country aggregators (db.country_aggregates and the legacy
-- country_mention_counts / country_timeline / country_cooccurrence /
-- items_for_country) all funnel through _classified_window_rows, whose only
-- sargable predicate is classified_at. This partial index lets the planner
-- range-scan exactly the classified-and-country-tagged rows.
--
-- NOTE: this is a MINOR optimisation, not the fix for the publish hang. On
-- current data nearly every classified-in-window row already carries a
-- country focus, so the partial predicate filters out little (measured ~14%
-- faster on the single scan). The hang fix is the single-pass aggregator
-- (db.country_aggregates) that replaces ~2N+2 window scans with 1. This index
-- is kept because it is harmless, forward-compatible, and helps more if the
-- no-country fraction grows.
--
-- Idempotent: IF NOT EXISTS guards against the index already existing (e.g.
-- created out-of-band on the live VM before this migration runs).

CREATE INDEX IF NOT EXISTS idx_items_country_window
    ON items(classified_at)
    WHERE country_focus_json IS NOT NULL AND country_focus_json != '[]';
