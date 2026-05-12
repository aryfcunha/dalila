-- Dalila — near-duplicate detection via title SimHash
--
-- Stored as TEXT (zero-padded 16-char hex of a 64-bit unsigned value) because
-- SQLite's INTEGER is signed 64-bit and we don't want sign-bit weirdness on
-- the high bit. Comparison is by Hamming distance over the bit representation,
-- not by lexical TEXT comparison, so the column type is just storage.

ALTER TABLE items ADD COLUMN title_simhash TEXT;

CREATE INDEX IF NOT EXISTS idx_items_title_simhash ON items(title_simhash);
