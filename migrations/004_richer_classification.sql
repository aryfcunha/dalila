-- Dalila — richer classification per UAE Foreign Aid Policy 2026
--
-- Adds three orthogonal axes to the existing classification (which kept
-- `category` + `uae_relevance` + `severity`):
--   1. policy_sector — the policy's sectoral taxonomy (water, food, energy,
--      natural-resources, health, education, trade, biz-enablement, ai-tech,
--      soe, humanitarian, financing, convening). Lets the digest carve out
--      "this week in health" / "this week in water" cross-cuts.
--   2. country_focus — ISO-3166-1 alpha-2 codes of the item's primary
--      geographies (typically 1-3). Powers /region and per-country views.
--   3. capital_signals — booleans flagging mentions of specific
--      continuum-of-capital instruments (Ch 8 of the policy): blended
--      finance, debt swaps, first-loss, guarantees, results-based.
--
-- Plus a graduation_signal flag for the self-reliance / transition theme.
--
-- Plus two extracted-entity tables:
--   - financial_commitments: pledges, MoUs, disbursements (amounts + currencies)
--   - bilateral_meetings: calls / visits / summits between UAE and foreign leaders

ALTER TABLE items ADD COLUMN policy_sector TEXT;
ALTER TABLE items ADD COLUMN country_focus_json TEXT;       -- JSON array of ISO-3166-1 alpha-2
ALTER TABLE items ADD COLUMN capital_signals_json TEXT;     -- JSON object of bool flags
ALTER TABLE items ADD COLUMN graduation_signal INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_items_policy_sector ON items(policy_sector);

-- Financial commitments extracted from item bodies. One row per commitment;
-- a single item can produce zero or many.
CREATE TABLE IF NOT EXISTS financial_commitments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id         INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    amount          REAL,
    currency        TEXT,            -- 'USD', 'AED', 'EUR', etc.
    fund_name       TEXT,            -- e.g. 'Loss and Damage Fund', 'Lives & Livelihoods Fund 2.0'
    recipient       TEXT,            -- country or recipient organisation
    commitment_type TEXT,            -- pledge | disbursement | mou | loan | grant
    announced_at    TEXT,            -- ISO8601 UTC; falls back to item ingested_at
    rationale       TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fc_item ON financial_commitments(item_id);
CREATE INDEX IF NOT EXISTS idx_fc_announced ON financial_commitments(announced_at);

-- Bilateral meetings, calls, and visits between UAE leadership and foreign
-- counterparts. The doctrine-tracker uses these for relationship mapping;
-- /meetings exposes them to the user as a ledger of high-level contacts.
CREATE TABLE IF NOT EXISTS bilateral_meetings (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id            INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    uae_principal      TEXT,         -- e.g. 'Mohamed bin Zayed Al Nahyan'
    foreign_principal  TEXT,
    foreign_country    TEXT,         -- ISO-3166-1 alpha-2 if identifiable
    meeting_type       TEXT,         -- call | meeting | visit | summit | bilateral
    when_iso           TEXT,         -- ISO8601 UTC if known; null otherwise
    location           TEXT,         -- city / country / venue
    topics_json        TEXT,         -- JSON array of topic strings
    created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bm_item ON bilateral_meetings(item_id);
CREATE INDEX IF NOT EXISTS idx_bm_when ON bilateral_meetings(when_iso);
CREATE INDEX IF NOT EXISTS idx_bm_country ON bilateral_meetings(foreign_country);
