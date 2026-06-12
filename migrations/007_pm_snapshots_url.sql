-- Migration 007: add url column to prediction_market_snapshots
--
-- The 2026-05-14 "Prediction Market Expansion" code change started writing a
-- url column in prediction_markets._upsert() and reading s.url in
-- get_market_signals(), but no migration ever added the column. Every
-- scheduled poll since then crashed with "table prediction_market_snapshots
-- has no column named url", so prediction_market_history stopped growing on
-- 2026-05-14 and the site rendered stale snapshot == stale prior = 0.0%
-- deltas for every market. This migration closes the schema drift.

ALTER TABLE prediction_market_snapshots ADD COLUMN url TEXT;
