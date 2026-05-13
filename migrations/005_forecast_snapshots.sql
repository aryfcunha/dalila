-- Migration 005: forecast_snapshots
--
-- Keys a per-(source, country, metric) "last observation" so forecast
-- ingestors (CAST, INFORM, HungerMap, GDACS) can decide whether the new
-- reading is a *change* worth surfacing in the brief.
--
-- Rule: never emit an item just because Sudan is hungry today (it's hungry
-- every day). Only emit when the metric moves enough to matter — the
-- ingestor compares against the row in this table, decides "yes change",
-- inserts the item, and updates the snapshot.
--
-- Schema is deliberately metric-agnostic (one row per source × country ×
-- metric_key), so the same table serves CAST risk scores, INFORM severity,
-- HungerMap "people with insufficient food", and GDACS alert severities.

CREATE TABLE IF NOT EXISTS forecast_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       TEXT NOT NULL,                  -- e.g. 'cast', 'inform', 'gdacs', 'hungermap'
    country_iso2    TEXT NOT NULL,                  -- ISO 3166-1 alpha-2
    metric_key      TEXT NOT NULL,                  -- e.g. 'risk_score', 'severity', 'food_insecure_count'
    value_num       REAL,                           -- the observation (or NULL if categorical)
    value_text      TEXT,                           -- categorical value if not numeric (e.g. 'red' / 'orange')
    observed_at     TEXT NOT NULL,                  -- ISO8601 UTC — when the producer published it
    recorded_at     TEXT NOT NULL,                  -- ISO8601 UTC — when we ingested it
    notes           TEXT,                           -- producer's free-text context for this observation
    UNIQUE (source_id, country_iso2, metric_key)
);

CREATE INDEX IF NOT EXISTS idx_fs_source_country
    ON forecast_snapshots(source_id, country_iso2);
