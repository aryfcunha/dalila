-- Dalila — UAE foreign-aid doctrine tracker
--
-- Stores the structured representation of UAE positions surfaced by the
-- classifier (any item with classifier's `doctrine_relation` set). The
-- doctrine_facts table is a single canonical position per (topic, dimension)
-- — updates append to evolution_log_json rather than overwriting.
--
-- See concept-note Appendix B for the schema rationale.

CREATE TABLE IF NOT EXISTS doctrine_facts (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    topic                 TEXT NOT NULL,           -- normalised: 'conditionality', 'climate-finance', etc.
    position_summary      TEXT NOT NULL,           -- one-sentence current UAE position
    nuance                TEXT,                    -- caveats, exceptions, audience-specific qualifiers
    first_stated_at       TEXT NOT NULL,           -- ISO8601 UTC — date the position first appeared in our data
    last_confirmed_at     TEXT NOT NULL,           -- ISO8601 UTC — most recent item that reinforced/refined it
    evolution_log_json    TEXT NOT NULL,           -- JSON array of {at, relation, item_id, summary}
    source_item_ids_json  TEXT NOT NULL,           -- JSON array of int item ids
    confidence            REAL NOT NULL DEFAULT 0.5,  -- 0..1; drops on contradictions, rises on reinforcements
    UNIQUE(topic)
);

CREATE INDEX IF NOT EXISTS idx_doctrine_facts_topic ON doctrine_facts(topic);
CREATE INDEX IF NOT EXISTS idx_doctrine_facts_last_confirmed ON doctrine_facts(last_confirmed_at);

-- Track which items have been through the doctrine pass so we don't double-process.
-- NULL means not yet considered; non-null means processed (either applied or no-op).
ALTER TABLE items ADD COLUMN doctrine_processed_at TEXT;

CREATE INDEX IF NOT EXISTS idx_items_doctrine_pending
    ON items(classified_at, doctrine_processed_at)
    WHERE doctrine_relation IS NOT NULL AND doctrine_processed_at IS NULL;
