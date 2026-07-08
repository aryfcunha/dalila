-- Migration 009: commitment provenance (source -> beneficiary).
--
-- Broadens financial_commitments from UAE-donor-centric (donor implicit, only a
-- free-text `recipient`) into a directed development-finance edge: who is giving
-- (source_country / source_entity) -> who is receiving (beneficiary_country /
-- beneficiary_entity). This is the telemetry for a future commitment-flows page.
--
-- All columns nullable; fully backward-compatible. Existing rows keep `recipient`
-- (now superseded by beneficiary_* but retained and COALESCE'd at read time).
-- Forward-only; idempotent under the hardened init_db (treats "duplicate
-- column" as already-applied).

ALTER TABLE financial_commitments ADD COLUMN source_country TEXT;
ALTER TABLE financial_commitments ADD COLUMN source_entity TEXT;
ALTER TABLE financial_commitments ADD COLUMN beneficiary_country TEXT;
ALTER TABLE financial_commitments ADD COLUMN beneficiary_entity TEXT;

-- Supports the future flows page (group by giver/receiver).
CREATE INDEX IF NOT EXISTS idx_fc_flow
    ON financial_commitments(source_country, beneficiary_country);
