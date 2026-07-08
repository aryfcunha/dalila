"""SQLite helpers: schema bootstrap, item insertion, queries."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse, urlunparse

from dalila.config import get_config, load_sources
from dalila.models import Classification, RawItem
from dalila.simhash import simhash64, to_hex


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalise_url(url: str) -> str:
    """Strip tracking params and fragments before hashing."""
    if not url:
        return ""
    try:
        p = urlparse(url.strip())
        # Drop query and fragment — tracking params shouldn't break dedup.
        cleaned = urlunparse((p.scheme, p.netloc.lower(), p.path.rstrip("/"), "", "", ""))
        return cleaned
    except Exception:
        return url.strip()


def url_hash(url: str | None, title: str = "") -> str:
    """Stable dedup key. Falls back to title hash if URL is missing."""
    if url:
        canonical = _normalise_url(url)
        if canonical:
            return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return hashlib.sha256(("titleonly:" + title.strip().lower()).encode("utf-8")).hexdigest()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    cfg = get_config()
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cfg.db_path, isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    # Wait up to 15s for a write lock instead of immediately raising
    # "database is locked". The scheduler runs ingest, classify and doctrine
    # concurrently against this single file; a long classify batch could hold
    # the write lock just as an ingest tick tries to insert, which dropped that
    # ingest batch with an OperationalError. WAL already lets readers run during
    # a write — this lets a competing *writer* block briefly and retry rather
    # than fail outright.
    conn.execute("PRAGMA busy_timeout = 15000")
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    """Apply schema migrations and seed the sources table from sources.yaml.

    Migrations are applied in filename order and tracked in `schema_migrations`
    so re-runs are idempotent. Add new migrations as `00X_*.sql`; never edit
    a migration that's already shipped to a live DB.
    """
    cfg = get_config()
    with connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "  filename TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {r["filename"] for r in conn.execute("SELECT filename FROM schema_migrations")}
        for sql_path in sorted(cfg.migrations_dir.glob("*.sql")):
            if sql_path.name in applied:
                continue
            try:
                conn.executescript(sql_path.read_text(encoding="utf-8"))
            except sqlite3.OperationalError as exc:
                # The migration's change is already present — e.g. a column was
                # added out-of-band, or schema_migrations lost this row after a
                # DB snapshot/restore. Treat "already applied" as success and
                # record it. Without this, ONE un-recorded ALTER (the live
                # 007 `ADD COLUMN url` did exactly this) makes init_db raise on
                # every call, which bricks the bot boot, publish-site, and every
                # CLI command permanently — with no operator around to fix it.
                msg = str(exc).lower()
                if "duplicate column name" in msg or "already exists" in msg:
                    import logging
                    logging.getLogger(__name__).warning(
                        "migration %s already applied (%s) — recording as done",
                        sql_path.name, exc,
                    )
                else:
                    raise
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(filename, applied_at) VALUES(?, ?)",
                (sql_path.name, _now_iso()),
            )
        _seed_sources(conn)
        n = backfill_title_simhash(conn)
        if n:
            import logging
            logging.getLogger(__name__).info("backfilled title_simhash for %d existing items", n)


def _seed_sources(conn: sqlite3.Connection) -> None:
    for src in load_sources():
        conn.execute(
            """
            INSERT INTO sources(id, name, kind, url, quality, enabled)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                kind=excluded.kind,
                url=excluded.url,
                quality=excluded.quality,
                enabled=excluded.enabled
            """,
            (
                src["id"],
                src["name"],
                src["kind"],
                src.get("url") or None,
                int(src.get("quality", 3)),
                1 if src.get("enabled", True) else 0,
            ),
        )


import re

def clean_title(title: str) -> str:
    from dalila.models import title_case_clean

    new_title = (title or "").strip()

    # OCHA/ReliefWeb geo-prefix: "oPt:" = occupied Palestinian territory
    new_title = re.sub(r'^[Oo][Pp][Tt]\s*[-:–—]\s*', '', new_title)

    # Generic "Opt:" prefix (some feeds use it as a content-type tag)
    new_title = re.sub(r'^Opt:\s*', '', new_title, flags=re.IGNORECASE)

    # GDELT article-ID tokens that leak into slug-derived titles.
    # Format 1: "by[4-8 alphanumeric] " (e.g. "byftehv", "by2pv0t")
    # Format 2: "[digit][4-7 alphanumeric] " (e.g. "1705hyd", "175byuy")
    new_title = re.sub(r'^by[a-z0-9]{4,8}\s+', '', new_title, flags=re.IGNORECASE)
    new_title = re.sub(r'^\d[a-z0-9]{4,7}\s+', '', new_title, flags=re.IGNORECASE)

    # Trailing numeric noise (scores, GDELT IDs, floats)
    new_title = re.sub(r'[\s\-–—]+\d+\.\d+\s*$', '', new_title)   # trailing float
    new_title = re.sub(r'[\s\-–—]+\d{4,}\s*$', '', new_title)      # trailing long int

    # Long floats / hex identifiers anywhere mid-title
    new_title = re.sub(r'\b\d+\.\d{5,}\b', '', new_title)
    new_title = re.sub(r'\b\d{8,}\b', '', new_title)
    new_title = re.sub(r'\b[a-f0-9]{12,}\b', '', new_title, flags=re.IGNORECASE)

    # CMS path-suffixes that survive slug extraction (".cms" = Times of India
    # family, ".ece" = Escenic). Seen verbatim as whole titles in digests.
    new_title = re.sub(r'\.(cms|ece|shtml|amp)\b', '', new_title, flags=re.IGNORECASE)

    new_title = re.sub(r'\s+', ' ', new_title).strip()

    # Full title-case when the title opens in lowercase
    if new_title and new_title[0].islower():
        new_title = title_case_clean(new_title)

    # Final gate: a usable headline has at least two alphabetic words. Bare
    # article ids ("1396974"), leftover extensions, or empty husks become ""
    # — items_for_digest() already excludes empty titles, so such items are
    # stored (for dedup/audit) but can never reach a digest or the site.
    if len(re.findall(r'[A-Za-z]{2,}', new_title)) < 2:
        return ""

    return new_title

def insert_item(conn: sqlite3.Connection, item: RawItem, prefilter_passed: bool) -> int | None:
    """Insert a new item; return the row id, or None if it was a duplicate."""
    item.title = clean_title(item.title)
    h = url_hash(item.url, item.title)
    sh = to_hex(simhash64(item.title))
    try:
        cur = conn.execute(
            """
            INSERT INTO items(
                source_id, url, url_hash, title, body, author,
                published_at, ingested_at, prefilter_passed, title_simhash
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.source_id,
                item.url,
                h,
                item.title.strip(),
                (item.body or "").strip() or None,
                item.author,
                item.published_at.isoformat() if item.published_at else None,
                _now_iso(),
                1 if prefilter_passed else 0,
                sh,
            ),
        )
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None  # url_hash UNIQUE violation = duplicate


def mark_source_polled(conn: sqlite3.Connection, source_id: str, items_seen: int, error: str | None = None) -> None:
    conn.execute(
        """
        UPDATE sources
        SET last_polled_at = ?, last_error = ?, items_seen = items_seen + ?
        WHERE id = ?
        """,
        (_now_iso(), error, items_seen, source_id),
    )


def unclassified_items(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    """Pull the next batch of items to classify, prioritised by source quality.

    Ordering: source.quality DESC, then prefers items with substantive body
    (>= 200 chars), then ingested_at DESC. This ensures high-signal sources
    (UAE state, ReliefWeb, NYT) get classified before noisier ones (GDELT's
    URL-slug titles) when classify budget is limited.
    """
    return list(conn.execute(
        """
        SELECT i.id, i.source_id, i.title, i.body, i.url, i.published_at
        FROM items i
        JOIN sources s ON s.id = i.source_id
        WHERE i.classified_at IS NULL
          AND i.prefilter_passed = 1
        ORDER BY s.quality DESC,
                 CASE WHEN LENGTH(COALESCE(i.body, '')) >= 200 THEN 1 ELSE 0 END DESC,
                 i.ingested_at DESC
        LIMIT ?
        """,
        (limit,),
    ))


def save_classification(conn: sqlite3.Connection, item_id: int, c: Classification) -> None:
    """Persist a Classification, including the migration-004 richer fields.

    Also writes derived financial_commitments and bilateral_meetings rows.
    The item's classified_at timestamp anchors fall-back dates for
    commitments/meetings that didn't carry their own when_iso/announced_at.
    """
    now = _now_iso()
    conn.execute(
        """
        UPDATE items SET
            classified_at = ?,
            category = ?,
            uae_relevance = ?,
            severity = ?,
            is_breaking_candidate = ?,
            doctrine_relation = ?,
            one_line_summary = ?,
            rationale = ?,
            entities_json = ?,
            policy_sector = ?,
            country_focus_json = ?,
            capital_signals_json = ?,
            graduation_signal = ?,
            title = COALESCE(?, title),
            title_simhash = CASE WHEN ? IS NOT NULL THEN ? ELSE title_simhash END,
            classifier_error = NULL
        WHERE id = ?
        """,
        (
            now,
            c.category,
            c.uae_relevance,
            c.severity,
            1 if c.is_breaking_candidate else 0,
            c.doctrine_relation,
            c.one_line_summary,
            c.rationale,
            json.dumps(c.entities, ensure_ascii=False),
            c.policy_sector,
            json.dumps(c.country_focus, ensure_ascii=False) if c.country_focus else None,
            json.dumps(c.capital_signals, ensure_ascii=False) if c.capital_signals else None,
            1 if c.graduation_signal else 0,
            clean_title(c.translated_title) if c.translated_title else None,
            clean_title(c.translated_title) if c.translated_title else None,
            to_hex(simhash64(clean_title(c.translated_title))) if c.translated_title else None,
            item_id,
        ),
    )

    # Re-insert commitments + meetings idempotently. Re-classifying an item
    # would otherwise duplicate them; delete-then-insert keeps the row count
    # honest. Cheap — most items have 0 of each.
    if c.financial_commitments or c.bilateral_meetings:
        conn.execute("DELETE FROM financial_commitments WHERE item_id = ?", (item_id,))
        conn.execute("DELETE FROM bilateral_meetings WHERE item_id = ?", (item_id,))
        for fc in c.financial_commitments:
            conn.execute(
                """INSERT INTO financial_commitments(
                       item_id, amount, currency, fund_name, recipient,
                       commitment_type, announced_at, rationale, created_at,
                       source_country, source_entity,
                       beneficiary_country, beneficiary_entity)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (item_id, fc.amount, fc.currency, fc.fund_name, fc.recipient,
                 fc.commitment_type, fc.announced_at, fc.rationale, now,
                 fc.source_country, fc.source_entity,
                 fc.beneficiary_country, fc.beneficiary_entity),
            )
        for bm in c.bilateral_meetings:
            conn.execute(
                """INSERT INTO bilateral_meetings(
                       item_id, uae_principal, foreign_principal, foreign_country,
                       meeting_type, when_iso, location, topics_json, created_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (item_id, bm.uae_principal, bm.foreign_principal, bm.foreign_country,
                 bm.meeting_type, bm.when_iso, bm.location,
                 json.dumps(bm.topics, ensure_ascii=False), now),
            )


def save_classifier_error(conn: sqlite3.Connection, item_id: int, error: str) -> None:
    conn.execute(
        "UPDATE items SET classifier_error = ?, classified_at = ? WHERE id = ?",
        (error[:1000], _now_iso(), item_id),
    )


def items_for_digest(
    conn: sqlite3.Connection,
    since_hours: int = 24,
    min_relevance: float = 0.4,
    *,
    as_of: datetime | None = None,
    exclude_ids: set[int] | None = None,
    per_outlet_cap: int = 4,
) -> list[dict]:
    """Items classified within the [as_of − since_hours, as_of] window.

    Two filters applied here that the SQL layer should enforce (cheap and
    deterministic, doesn't need the editor to dance around bad data):
      * Items whose title is essentially just a URL are dropped (these come
        from GDELT and a few RSS feeds with broken title fields; classifier
        wastes a slot on them).
      * Items in `exclude_ids` are dropped — used by run_backfill_digests
        so the same item never appears in two consecutive briefs.

    `per_outlet_cap` bounds how many candidates a single publisher (by URL
    domain — NOT source_id, since all of GDELT shares one source_id) may
    contribute, in rank order. Without it the highest-volume papers (e.g.
    the Indian dailies that dominate GDELT's English feed) crowd out every
    other outlet. 0 or None disables the cap.

    `as_of` defaults to "now" (UTC). Set it to a past datetime for backfill —
    e.g. to compose yesterday's digest, pass `as_of=yesterday-06:30-utc`.
    """
    end = as_of or datetime.now(timezone.utc)
    start = end - timedelta(hours=since_hours)

    exclude_clause = ""
    params: list = [start.isoformat(), end.isoformat(), min_relevance]
    if exclude_ids:
        placeholders = ",".join("?" * len(exclude_ids))
        exclude_clause = f" AND i.id NOT IN ({placeholders}) "
        params.extend(int(x) for x in exclude_ids)

    # The window is anchored on published_at (when the news actually happened
    # in the world), falling back to ingested_at for items whose source feed
    # didn't supply a date. RSS feeds carry items going back days, so news
    # published on May 9 and ingested on May 12 correctly belongs to the
    # May 10 brief — not the May 13 one. classified_at is irrelevant to the
    # window: re-classification doesn't change which day a story belongs to.
    rows = conn.execute(
        f"""
        SELECT i.id, i.title, i.url, i.body, i.one_line_summary, i.category,
               i.uae_relevance, i.severity, i.entities_json, i.doctrine_relation,
               i.title_simhash,
               s.name AS source_name, s.quality AS source_quality
        FROM items i
        JOIN sources s ON s.id = i.source_id
        WHERE i.classified_at IS NOT NULL
          AND COALESCE(i.published_at, i.ingested_at) >= ?
          AND COALESCE(i.published_at, i.ingested_at) <= ?
          AND COALESCE(i.uae_relevance, 0) >= ?
          AND i.category != 'other'
          AND TRIM(COALESCE(i.title, '')) != ''
          AND COALESCE(i.title, '') NOT LIKE 'http://%'
          AND COALESCE(i.title, '') NOT LIKE 'https://%'
          AND COALESCE(i.title, '') NOT LIKE 'www.%'
          {exclude_clause}
        ORDER BY (i.uae_relevance * COALESCE(i.severity, 0.5) * s.quality) DESC,
                 i.ingested_at DESC
        """,
        params,
    ).fetchall()
    out = []
    outlet_counts: dict[str, int] = {}
    for r in rows:
        domain = _outlet_domain(r["url"]) or r["source_name"]
        if per_outlet_cap:
            if outlet_counts.get(domain, 0) >= per_outlet_cap:
                continue
            outlet_counts[domain] = outlet_counts.get(domain, 0) + 1
        out.append({
            "id": r["id"],
            "title": r["title"],
            "url": r["url"],
            "summary": r["one_line_summary"] or (r["body"] or "")[:200],
            "category": r["category"],
            "source": r["source_name"],
            "uae_relevance": r["uae_relevance"],
            "severity": r["severity"],
            "entities": json.loads(r["entities_json"]) if r["entities_json"] else [],
            "doctrine_relation": r["doctrine_relation"],
            "title_simhash": r["title_simhash"],
            "_outlet": domain,
        })
    return _interleave_by_outlet(out)


def _interleave_by_outlet(ranked: list[dict]) -> list[dict]:
    """Breadth-first re-order: every outlet's best item before any outlet's
    second item, then every second item, and so on. Within a round, outlets
    keep the order their item earned by score — so quality still decides who
    leads, but the top of the candidate list spans as many publishers as the
    pool offers instead of letting two or three high-volume outlets fill the
    editor's window."""
    by_outlet: dict[str, list[dict]] = {}
    outlet_order: list[str] = []
    for item in ranked:
        key = item.pop("_outlet", "") or item.get("source", "")
        if key not in by_outlet:
            by_outlet[key] = []
            outlet_order.append(key)
        by_outlet[key].append(item)
    out: list[dict] = []
    round_i = 0
    while True:
        added = False
        for key in outlet_order:
            bucket = by_outlet[key]
            if round_i < len(bucket):
                out.append(bucket[round_i])
                added = True
        if not added:
            return out
        round_i += 1


def _outlet_domain(url: str | None) -> str:
    """Publisher domain for the outlet-diversity cap ("www."/port stripped)."""
    if not url:
        return ""
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def previously_digested_item_ids(
    conn: sqlite3.Connection, since_hours: int = 24 * 7
) -> set[int]:
    """Return the union of item-IDs that appeared in any digest composed in
    the last N hours. Used to suppress duplicates across consecutive briefs
    so the same story doesn't lead three mornings in a row."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
    out: set[int] = set()
    for r in conn.execute(
        "SELECT item_ids_json FROM digests WHERE composed_at >= ?",
        (cutoff,),
    ):
        try:
            for x in (json.loads(r["item_ids_json"]) or []):
                if x is not None:
                    out.add(int(x))
        except Exception:
            continue
    return out


def save_digest(conn: sqlite3.Connection, date_label: str, content: str, item_ids: list[int], *,
                force: bool = False) -> int:
    """Persist a digest.  If a digest already exists for `date_label` and
    `force` is False (the default), the existing id is returned unchanged so
    the scheduler never creates duplicate rows for the same day."""
    if not force:
        existing = conn.execute(
            "SELECT id FROM digests WHERE date_label = ? ORDER BY id DESC LIMIT 1",
            (date_label,),
        ).fetchone()
        if existing:
            return existing["id"]
    cur = conn.execute(
        """
        INSERT INTO digests(composed_at, date_label, content, item_ids_json, item_count)
        VALUES(?, ?, ?, ?, ?)
        """,
        (_now_iso(), date_label, content, json.dumps(item_ids), len(item_ids)),
    )
    return cur.lastrowid  # type: ignore[return-value]


def items_matching_topic(
    conn: sqlite3.Connection,
    topic: str,
    *,
    since_hours: int = 720,   # 30 days
    limit: int = 20,
) -> list[dict]:
    """Find classified items relevant to a free-text topic for the /more deep-dive.

    Matching strategy (cheap, no embeddings):
      1. Substring match against title, one_line_summary, body, or entities_json.
      2. Restrict to items with `classified_at` set (so we have summary + entities).
      3. Order by score (uae_relevance * severity * source_quality) DESC, then
         recency, so the most-consequential items come first.

    Returns the same shape as items_for_digest so the deep-dive composer can
    reuse the same numbering/payload format.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
    topic_norm = topic.strip().lower()
    if not topic_norm:
        return []
    like = f"%{topic_norm}%"
    rows = conn.execute(
        """
        SELECT i.id, i.title, i.url, i.body, i.one_line_summary, i.category,
               i.uae_relevance, i.severity, i.entities_json, i.doctrine_relation,
               i.title_simhash, i.ingested_at,
               s.name AS source_name, s.quality AS source_quality
        FROM items i
        JOIN sources s ON s.id = i.source_id
        WHERE i.classified_at IS NOT NULL
          AND i.classifier_error IS NULL
          AND i.ingested_at >= ?
          AND (LOWER(i.title) LIKE ?
               OR LOWER(COALESCE(i.one_line_summary, '')) LIKE ?
               OR LOWER(COALESCE(i.body, '')) LIKE ?
               OR LOWER(COALESCE(i.entities_json, '')) LIKE ?)
        ORDER BY (COALESCE(i.uae_relevance, 0.3) * COALESCE(i.severity, 0.3) * s.quality) DESC,
                 i.ingested_at DESC
        LIMIT ?
        """,
        (cutoff, like, like, like, like, limit),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        out.append({
            "id": r["id"],
            "title": r["title"],
            "url": r["url"],
            "summary": r["one_line_summary"] or (r["body"] or "")[:200],
            "category": r["category"],
            "source": r["source_name"],
            "uae_relevance": r["uae_relevance"],
            "severity": r["severity"],
            "entities": json.loads(r["entities_json"]) if r["entities_json"] else [],
            "doctrine_relation": r["doctrine_relation"],
            "title_simhash": r["title_simhash"],
            "ingested_at": r["ingested_at"],
        })
    return out


def backfill_title_simhash(conn: sqlite3.Connection, batch: int = 1000) -> int:
    """Compute title_simhash for any item missing it (post-migration backfill).

    Returns the number of rows updated. Safe to run repeatedly — only touches
    rows where the column is NULL. Called from `dalila init` so upgrades pick
    it up without manual intervention.
    """
    rows = conn.execute(
        "SELECT id, title FROM items WHERE title_simhash IS NULL"
    ).fetchall()
    if not rows:
        return 0
    updates = [(to_hex(simhash64(r["title"])), r["id"]) for r in rows]
    for i in range(0, len(updates), batch):
        conn.executemany(
            "UPDATE items SET title_simhash = ? WHERE id = ?", updates[i:i + batch]
        )
    return len(updates)


def recent_financial_commitments(conn: sqlite3.Connection, hours: int = 720, limit: int = 30) -> list[dict]:
    """Recent extracted commitments, newest first. Joins back to the source item."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    rows = conn.execute(
        """
        SELECT fc.id, fc.amount, fc.currency, fc.fund_name, fc.recipient,
               fc.commitment_type, fc.announced_at, fc.rationale, fc.created_at,
               fc.source_country, fc.source_entity,
               fc.beneficiary_country, fc.beneficiary_entity,
               i.id AS item_id, i.title, i.url
        FROM financial_commitments fc
        JOIN items i ON i.id = fc.item_id
        WHERE COALESCE(fc.announced_at, fc.created_at) >= ?
        ORDER BY COALESCE(fc.announced_at, fc.created_at) DESC, fc.id DESC
        LIMIT ?
        """,
        (cutoff, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _norm_principal(s: str | None) -> str:
    """Normalize a principal/country name for dedup keys: lower, punctuation→space
    (so 'Al-Nahyan' == 'Al Nahyan'), collapse whitespace."""
    import re
    s = re.sub(r"[^\w\s]", " ", (s or "").strip().lower())
    return re.sub(r"\s+", " ", s).strip()


def recent_bilateral_meetings(conn: sqlite3.Connection, hours: int = 720, limit: int = 30) -> list[dict]:
    """Recent bilateral meetings, DE-DUPLICATED at render time.

    N outlets covering the same meeting create N near-identical rows (there is
    no storage-level uniqueness, by design — the re-classify path deletes by
    item_id). We collapse them on a canonical key
    (norm uae_principal + norm foreign_principal + norm foreign_country +
    meeting DATE) so each real meeting shows once, with `mention_count` and the
    list of source articles.

    Guards (per the audit's adversarial review):
      - BOTH principals blank → keyed on row id, never merged (an unidentified
        meeting is a real distinct event, not a duplicate of other blanks).
      - The date component uses `when_iso` ONLY (not `created_at`), so two
        outlets that left when_iso NULL but ingested on different days still
        collapse — otherwise differing ingest dates would split a real dup.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    # Pull a generous superset (duplicates collapse below), newest first.
    rows = conn.execute(
        """
        SELECT bm.id, bm.uae_principal, bm.foreign_principal, bm.foreign_country,
               bm.meeting_type, bm.when_iso, bm.location, bm.topics_json,
               bm.created_at, i.id AS item_id, i.title, i.url
        FROM bilateral_meetings bm
        JOIN items i ON i.id = bm.item_id
        WHERE COALESCE(bm.when_iso, bm.created_at) >= ?
        ORDER BY COALESCE(bm.when_iso, bm.created_at) DESC, bm.id DESC
        LIMIT 2000
        """,
        (cutoff,),
    ).fetchall()

    by_key: dict = {}
    order: list = []

    def _principals(r):
        """(uae, foreign, country) normalized, or None for blank-both."""
        uae = _norm_principal(r["uae_principal"])
        foreign = _norm_principal(r["foreign_principal"])
        if not uae and not foreign:
            return None
        return (uae, foreign, _norm_principal(r["foreign_country"]))

    def _add(r, key):
        src = {"title": r["title"], "url": r["url"], "item_id": r["item_id"]}
        if key in by_key:
            by_key[key]["mention_count"] += 1
            by_key[key]["sources"].append(src)
        elif len(order) < limit:
            d = dict(r)
            d["topics"] = json.loads(d.pop("topics_json")) if d.get("topics_json") else []
            d["mention_count"] = 1
            d["sources"] = [src]
            by_key[key] = d
            order.append(key)
        # else: already have `limit` distinct meetings — ignore new keys

    # Two passes so a date-less report still collapses with dated reports of the
    # same meeting: pass 1 buckets DATED rows by principals+day (newest first,
    # so the first dated key per principal set is the representative); pass 2
    # attaches NULL-date rows to that same-principals bucket, else makes their
    # own. Blank-both rows are always distinct (keyed on row id).
    principals_to_key: dict = {}
    dated = [r for r in rows if (r["when_iso"] or "")[:10]]
    undated = [r for r in rows if not (r["when_iso"] or "")[:10]]
    for r in dated:
        pk = _principals(r)
        if pk is None:
            _add(r, ("__blank__", r["id"]))
        else:
            key = pk + ((r["when_iso"] or "")[:10],)
            principals_to_key.setdefault(pk, key)
            _add(r, key)
    for r in undated:
        pk = _principals(r)
        if pk is None:
            _add(r, ("__blank__", r["id"]))
        else:
            key = principals_to_key.get(pk) or (pk + ("",))
            principals_to_key.setdefault(pk, key)
            _add(r, key)
    return [by_key[k] for k in order]


def items_by_country_codes(
    conn: sqlite3.Connection,
    codes: list[str],
    *,
    since_hours: int = 720,
    limit: int = 25,
) -> list[dict]:
    """Items whose country_focus_json contains ANY of the given ISO codes.

    Uses substring match against the stored JSON array text — sqlite has no
    native JSON list-containment without the JSON1 extension. With codes
    being exactly two uppercase letters wrapped in quotes in the JSON, false
    positives are extraordinarily unlikely.
    """
    if not codes:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
    clauses = " OR ".join(["i.country_focus_json LIKE ?"] * len(codes))
    params: list = [cutoff]
    params.extend([f'%"{c.upper()}"%' for c in codes])
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT i.id, i.title, i.url, i.one_line_summary, i.category,
               i.uae_relevance, i.severity, i.entities_json, i.country_focus_json,
               i.policy_sector, i.ingested_at,
               s.name AS source_name, s.quality AS source_quality
        FROM items i
        JOIN sources s ON s.id = i.source_id
        WHERE i.classified_at IS NOT NULL
          AND i.classifier_error IS NULL
          AND i.ingested_at >= ?
          AND ({clauses})
        ORDER BY (COALESCE(i.uae_relevance, 0.3) * COALESCE(i.severity, 0.3) * s.quality) DESC,
                 i.ingested_at DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        out.append({
            "id": r["id"],
            "title": r["title"],
            "url": r["url"],
            "summary": r["one_line_summary"] or "",
            "category": r["category"],
            "source": r["source_name"],
            "uae_relevance": r["uae_relevance"],
            "severity": r["severity"],
            "policy_sector": r["policy_sector"],
            "country_focus": json.loads(r["country_focus_json"]) if r["country_focus_json"] else [],
            "ingested_at": r["ingested_at"],
        })
    return out


def doctrine_velocity_snapshot(conn: sqlite3.Connection, hours: int = 720, limit: int = 5) -> list[dict]:
    """Return topics with the most evolution_log activity in the window.

    Used by /status to surface 'doctrine in motion'. We count log entries
    by parsing JSON in Python rather than relying on SQLite JSON1 (not always
    compiled in on Debian/Ubuntu).
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    rows = conn.execute(
        "SELECT topic, evolution_log_json, confidence FROM doctrine_facts"
    ).fetchall()
    out: list[tuple[str, int, float]] = []
    for r in rows:
        log_arr = json.loads(r["evolution_log_json"]) if r["evolution_log_json"] else []
        recent = sum(1 for e in log_arr if (e.get("at") or "") >= cutoff)
        if recent:
            out.append((r["topic"], recent, float(r["confidence"])))
    out.sort(key=lambda t: (-t[1], t[0]))
    return [{"topic": t, "updates": n, "confidence": c} for t, n, c in out[:limit]]


def items_pending_doctrine(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    """Classified items with a doctrine_relation that haven't been through the doctrine pass yet."""
    return list(conn.execute(
        """
        SELECT id, title, body, one_line_summary, doctrine_relation,
               entities_json, ingested_at, uae_relevance
        FROM items
        WHERE classified_at IS NOT NULL
          AND classifier_error IS NULL
          AND doctrine_relation IS NOT NULL
          AND doctrine_relation NOT IN ('', 'null')
          AND doctrine_processed_at IS NULL
        ORDER BY ingested_at DESC
        LIMIT ?
        """,
        (limit,),
    ))


def mark_item_doctrine_processed(conn: sqlite3.Connection, item_id: int) -> None:
    conn.execute(
        "UPDATE items SET doctrine_processed_at = ? WHERE id = ?",
        (_now_iso(), item_id),
    )


def list_doctrine_facts(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT id, topic, position_summary, nuance, first_stated_at,
                  last_confirmed_at, evolution_log_json, source_item_ids_json,
                  confidence
           FROM doctrine_facts
           ORDER BY last_confirmed_at DESC"""
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        out.append({
            "id": r["id"],
            "topic": r["topic"],
            "position_summary": r["position_summary"],
            "nuance": r["nuance"],
            "first_stated_at": r["first_stated_at"],
            "last_confirmed_at": r["last_confirmed_at"],
            "evolution_log": json.loads(r["evolution_log_json"]) if r["evolution_log_json"] else [],
            "source_item_ids": json.loads(r["source_item_ids_json"]) if r["source_item_ids_json"] else [],
            "confidence": r["confidence"],
        })
    return out


def doctrine_fact_for_topic(conn: sqlite3.Connection, topic: str) -> dict | None:
    facts = [f for f in list_doctrine_facts(conn) if f["topic"] == topic]
    return facts[0] if facts else None


def upsert_doctrine_fact_new(
    conn: sqlite3.Connection,
    *,
    topic: str,
    position_summary: str,
    nuance: str | None,
    source_item_id: int,
    initial_confidence: float = 0.5,
) -> int:
    """Insert a fresh doctrine fact. Returns the new row id.

    Initialises evolution_log with the originating item and timestamps with now.
    """
    now = _now_iso()
    entry = [{
        "at": now,
        "relation": "new",
        "item_id": source_item_id,
        "summary": "First recorded statement on this topic.",
    }]
    cur = conn.execute(
        """
        INSERT INTO doctrine_facts(
            topic, position_summary, nuance, first_stated_at, last_confirmed_at,
            evolution_log_json, source_item_ids_json, confidence
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(topic) DO NOTHING
        """,
        (topic, position_summary, nuance, now, now,
         json.dumps(entry), json.dumps([source_item_id]), initial_confidence),
    )
    if cur.rowcount == 0:
        # Concurrent insert — fall back to append
        return _append_doctrine_entry(
            conn, topic=topic, position_summary=position_summary, nuance=nuance,
            evolution_entry={"relation": "reinforcing", "summary": "Concurrent insert; merged."},
            source_item_id=source_item_id, confidence_delta=0.0,
        )
    row = conn.execute("SELECT id FROM doctrine_facts WHERE topic = ?", (topic,)).fetchone()
    return int(row["id"])


def _append_doctrine_entry(
    conn: sqlite3.Connection,
    *,
    topic: str,
    position_summary: str,
    nuance: str | None,
    evolution_entry: dict,
    source_item_id: int,
    confidence_delta: float,
) -> int:
    """Append an evolution log entry to an existing topic; update position + confidence."""
    row = conn.execute(
        "SELECT id, evolution_log_json, source_item_ids_json, confidence FROM doctrine_facts WHERE topic = ?",
        (topic,),
    ).fetchone()
    if not row:
        return upsert_doctrine_fact_new(
            conn, topic=topic, position_summary=position_summary,
            nuance=nuance, source_item_id=source_item_id,
        )
    log_arr = json.loads(row["evolution_log_json"]) if row["evolution_log_json"] else []
    src_arr = json.loads(row["source_item_ids_json"]) if row["source_item_ids_json"] else []
    now = _now_iso()
    log_arr.append({
        "at": now,
        "relation": evolution_entry.get("relation") or "reinforcing",
        "item_id": source_item_id,
        "summary": evolution_entry.get("summary") or "",
    })
    if source_item_id not in src_arr:
        src_arr.append(source_item_id)
    new_conf = max(0.0, min(1.0, float(row["confidence"]) + float(confidence_delta)))
    conn.execute(
        """UPDATE doctrine_facts SET
               position_summary = ?,
               nuance = COALESCE(?, nuance),
               last_confirmed_at = ?,
               evolution_log_json = ?,
               source_item_ids_json = ?,
               confidence = ?
           WHERE topic = ?""",
        (position_summary, nuance, now, json.dumps(log_arr), json.dumps(src_arr), new_conf, topic),
    )
    return int(row["id"])


def append_doctrine_entry(
    conn: sqlite3.Connection,
    *,
    topic: str,
    position_summary: str,
    nuance: str | None,
    evolution_entry: dict,
    source_item_id: int,
    confidence_delta: float,
) -> int:
    """Public alias of _append_doctrine_entry for the doctrine module."""
    return _append_doctrine_entry(
        conn, topic=topic, position_summary=position_summary, nuance=nuance,
        evolution_entry=evolution_entry, source_item_id=source_item_id,
        confidence_delta=confidence_delta,
    )


def latest_digest(conn: sqlite3.Connection) -> dict | None:
    """Return the most recently composed digest (id, date_label, content, item_ids, composed_at), or None."""
    row = conn.execute(
        "SELECT id, composed_at, date_label, content, item_ids_json "
        "FROM digests ORDER BY composed_at DESC, id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "composed_at": row["composed_at"],
        "date_label": row["date_label"],
        "content": row["content"],
        "item_ids": json.loads(row["item_ids_json"]) if row["item_ids_json"] else [],
    }


def digest_exists_for_label(conn: sqlite3.Connection, date_label: str) -> bool:
    """True if a digest has already been persisted for this human date_label.

    Used by the scheduler's startup catch-up to decide whether today's brief
    still needs composing — without this guard the catch-up would re-broadcast
    a brief the daily cron job already sent."""
    row = conn.execute(
        "SELECT 1 FROM digests WHERE date_label = ? LIMIT 1", (date_label,)
    ).fetchone()
    return row is not None


def maintain_database() -> dict:
    """Periodic upkeep so the DB stays healthy over months of unattended runs.

    1. `wal_checkpoint(TRUNCATE)` — flush committed WAL frames back into the
       main file and shrink the -wal to zero. Between-batch checkpoints already
       run on the hot path, but a long-held reader (e.g. an in-flight publish)
       can defer truncation; this nightly pass guarantees the WAL never carries
       drift forward indefinitely, which is what filled the disk to 95%.
    2. `PRAGMA optimize` — refresh the query planner's stats. Cheap, recommended
       by SQLite to run periodically on long-lived databases.

    Returns a small summary dict for logging. Never raises — maintenance is
    best-effort and must not take the bot down.
    """
    import logging
    log = logging.getLogger(__name__)
    result = {"checkpoint": None, "optimized": False}
    try:
        with connect() as conn:
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            # row = (busy, log_frames, checkpointed_frames); busy==0 means success
            result["checkpoint"] = tuple(row) if row is not None else None
            conn.execute("PRAGMA optimize")
            result["optimized"] = True
    except Exception:
        log.exception("maintain_database: upkeep failed (non-fatal)")
    return result


def get_url_for_item(conn: sqlite3.Connection, item_id: int) -> str | None:
    row = conn.execute("SELECT url FROM items WHERE id = ?", (item_id,)).fetchone()
    return row["url"] if row else None


def set_cluster_id(conn: sqlite3.Connection, item_id: int, cluster_id: int) -> None:
    """Tag an item with a same-event cluster id (the representative item's id).
    Used by the semantic de-dup pass; reuses the existing items.cluster_id col."""
    conn.execute("UPDATE items SET cluster_id = ? WHERE id = ?", (cluster_id, item_id))


# ---------------------------------------------------------------------------
# Country / region aggregations — power the /country command + country view
# ---------------------------------------------------------------------------

def _classified_window_rows(conn: sqlite3.Connection, since_hours: int) -> list[sqlite3.Row]:
    """Return id + country_focus_json + ingested_at + title etc. for items
    classified in the last N hours (used by the country aggregators).
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
    return list(conn.execute(
        """SELECT i.id, i.title, i.url, i.one_line_summary, i.published_at,
                  i.ingested_at, i.uae_relevance, i.severity, i.category,
                  i.country_focus_json,
                  s.name AS source_name
           FROM items i LEFT JOIN sources s ON s.id = i.source_id
           WHERE i.classified_at IS NOT NULL
             AND i.classified_at >= ?
             AND i.country_focus_json IS NOT NULL
             AND i.country_focus_json != '[]'
             -- Development-relevance floor for the country page: the classifier
             -- forces sports / celebrity / trivia hard-negatives to
             -- category='other' (see prompts/classifier.md), so excluding
             -- 'other' drops that junk from counts, the heatmap, the timeline,
             -- co-occurrence and per-country news in one place. Legitimate dev
             -- categories are never 'other', so this carries no false-negative
             -- risk. (The digest path enforces its own relevance gate already.)
             AND i.category != 'other'
             AND TRIM(COALESCE(i.title, '')) != ''
             AND COALESCE(i.title, '') NOT LIKE 'http://%'
             AND COALESCE(i.title, '') NOT LIKE 'https://%'
             AND COALESCE(i.title, '') NOT LIKE 'www.%'""",
        (cutoff,),
    ))


def country_mention_counts(conn: sqlite3.Connection, since_hours: int = 720) -> dict[str, int]:
    """{ISO-2: number of items mentioning that country in the window}. Default 30 days."""
    counts: dict[str, int] = {}
    for r in _classified_window_rows(conn, since_hours):
        try:
            for iso in (json.loads(r["country_focus_json"]) or []):
                iso2 = str(iso).strip().upper()
                if len(iso2) == 2:
                    counts[iso2] = counts.get(iso2, 0) + 1
        except Exception:
            continue
    return counts


def country_timeline(
    conn: sqlite3.Connection, since_hours: int = 24 * 180,
) -> dict[str, dict[str, int]]:
    """Per-day per-country mention counts for the timeline chips on the
    country-view page.

    Returns {YYYY-MM-DD: {ISO-2: count}}. The date used is the item's
    `published_at` (when the news happened), falling back to `ingested_at`
    for sources that don't supply one — same semantic as `items_for_digest`.

    The browser-side filter slices this map by date range when the user picks
    a chip (7d / 30d / 90d / all). One server-side aggregate, infinite
    re-slicing on the client.
    """
    out: dict[str, dict[str, int]] = {}
    for r in _classified_window_rows(conn, since_hours):
        try:
            isos = [str(c).strip().upper() for c in (json.loads(r["country_focus_json"]) or [])]
        except Exception:
            continue
        when = (r["published_at"] or r["ingested_at"] or "")[:10]
        if len(when) != 10:
            continue
        bucket = out.setdefault(when, {})
        for iso in isos:
            if len(iso) == 2 and iso.isalpha():
                bucket[iso] = bucket.get(iso, 0) + 1
    return out


def country_cooccurrence(
    conn: sqlite3.Connection, iso2: str, since_hours: int = 720
) -> dict[str, int]:
    """For items that mention `iso2`, count co-mentions of every other country.
    Returns {other_iso: count}, descending order preserved by sort at call site.
    """
    iso2 = (iso2 or "").upper()
    if len(iso2) != 2:
        return {}
    co: dict[str, int] = {}
    for r in _classified_window_rows(conn, since_hours):
        try:
            countries = [str(c).strip().upper() for c in (json.loads(r["country_focus_json"]) or [])]
        except Exception:
            continue
        if iso2 not in countries:
            continue
        for other in countries:
            if other == iso2 or len(other) != 2:
                continue
            co[other] = co.get(other, 0) + 1
    return co


def items_for_country(
    conn: sqlite3.Connection, iso2: str, since_hours: int = 720, limit: int = 50,
) -> list[dict]:
    """Items mentioning `iso2`, newest classification first."""
    iso2 = (iso2 or "").upper()
    if len(iso2) != 2:
        return []
    out: list[dict] = []
    for r in _classified_window_rows(conn, since_hours):
        try:
            countries = [str(c).strip().upper() for c in (json.loads(r["country_focus_json"]) or [])]
        except Exception:
            continue
        if iso2 not in countries:
            continue
        out.append({
            "id": r["id"],
            "title": r["title"],
            "url": r["url"],
            "summary": r["one_line_summary"] or "",
            "category": r["category"],
            "uae_relevance": r["uae_relevance"],
            "severity": r["severity"],
            "published_at": r["published_at"],
            "ingested_at": r["ingested_at"],
            "source": r["source_name"],
            "country_focus": countries,
        })
    # Sort by published_at desc when available, ingested_at otherwise
    out.sort(key=lambda x: x.get("published_at") or x.get("ingested_at") or "", reverse=True)
    return out[:limit]


def country_aggregates(
    conn: sqlite3.Connection,
    since_hours: int = 24 * 180,
    *,
    items_limit: int = 30,
) -> dict:
    """Single-pass replacement for the per-country fan-out that hung publish-site.

    The countries page used to call country_mention_counts (1x) plus
    items_for_country + country_cooccurrence ONCE PER COUNTRY (~190 countries),
    each re-scanning the full classified window and re-parsing every row's
    country_focus_json — ~2N+2 full passes. On a 317k-row items table that is
    minutes of pure json.loads and a memory storm on the 1GB VM, i.e. a hang.

    This scans the window ONCE, parses each row's JSON ONCE, and fills every
    structure the page needs in the same loop:

        {
          "counts":           {ISO: n},                  # == country_mention_counts
          "timeline":         {YYYY-MM-DD: {ISO: n}},     # == country_timeline
          "cooccurrence":     {ISO: {other_ISO: n}},      # == country_cooccurrence per ISO
          "items_by_country": {ISO: [item dict, ...]},    # == items_for_country per ISO (top N)
        }

    Output matches the four legacy functions on all current data. It applies
    slightly stricter per-row validation uniformly (2-letter alpha ISO codes,
    de-duplicated within a row so a malformed row can't double-count
    co-occurrence) — the legacy functions varied on this; the stricter rule is
    a no-op today and more robust on future junk. The legacy functions are kept
    for the /country bot command, the country-debug CLI, and tests.
    """
    counts: dict[str, int] = {}
    timeline: dict[str, dict[str, int]] = {}
    cooccurrence: dict[str, dict[str, int]] = {}
    items_by_country: dict[str, list[dict]] = {}

    for r in _classified_window_rows(conn, since_hours):
        try:
            raw = json.loads(r["country_focus_json"]) or []
        except Exception:
            continue
        valid: list[str] = []
        seen: set[str] = set()
        for c in raw:
            iso = str(c).strip().upper()
            if len(iso) == 2 and iso.isalpha() and iso not in seen:
                seen.add(iso)
                valid.append(iso)
        if not valid:
            continue

        # counts
        for iso in valid:
            counts[iso] = counts.get(iso, 0) + 1

        # timeline: bucket by published_at|ingested_at date
        when = (r["published_at"] or r["ingested_at"] or "")[:10]
        if len(when) == 10:
            bucket = timeline.setdefault(when, {})
            for iso in valid:
                bucket[iso] = bucket.get(iso, 0) + 1

        # cooccurrence: every ordered pair within the row
        for iso in valid:
            co = cooccurrence.setdefault(iso, {})
            for other in valid:
                if other != iso:
                    co[other] = co.get(other, 0) + 1

        # per-country item lists (sorted + capped after the pass)
        item = {
            "id": r["id"],
            "title": r["title"],
            "url": r["url"],
            "summary": r["one_line_summary"] or "",
            "category": r["category"],
            "uae_relevance": r["uae_relevance"],
            "severity": r["severity"],
            "published_at": r["published_at"],
            "ingested_at": r["ingested_at"],
            "source": r["source_name"],
            "country_focus": valid,
        }
        for iso in valid:
            items_by_country.setdefault(iso, []).append(item)

    for iso, lst in items_by_country.items():
        lst.sort(key=lambda x: x.get("published_at") or x.get("ingested_at") or "", reverse=True)
        items_by_country[iso] = lst[:items_limit]

    return {
        "counts": counts,
        "timeline": timeline,
        "cooccurrence": cooccurrence,
        "items_by_country": items_by_country,
    }


def enabled_users(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM users WHERE enabled = 1"))


def upsert_user(conn: sqlite3.Connection, chat_id: int, username: str | None, first_name: str | None) -> None:
    conn.execute(
        """
        INSERT INTO users(chat_id, username, first_name, created_at, enabled)
        VALUES(?, ?, ?, ?, 1)
        ON CONFLICT(chat_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name,
            enabled = 1
        """,
        (chat_id, username, first_name, _now_iso()),
    )


def set_user_enabled(conn: sqlite3.Connection, chat_id: int, enabled: bool) -> None:
    conn.execute("UPDATE users SET enabled = ? WHERE chat_id = ?", (1 if enabled else 0, chat_id))


def record_delivery(conn: sqlite3.Connection, digest_id: int, chat_id: int, success: bool, error: str | None) -> None:
    conn.execute(
        "INSERT INTO deliveries(digest_id, chat_id, delivered_at, success, error) VALUES(?, ?, ?, ?, ?)",
        (digest_id, chat_id, _now_iso(), 1 if success else 0, error),
    )


def record_llm_call(conn: sqlite3.Connection, model: str, purpose: str, duration_ms: int, success: bool, error: str | None) -> None:
    conn.execute(
        "INSERT INTO llm_call_log(called_at, model, purpose, duration_ms, success, error) VALUES(?, ?, ?, ?, ?, ?)",
        (_now_iso(), model, purpose, duration_ms, 1 if success else 0, error),
    )


def todays_classifier_call_count(conn: sqlite3.Connection) -> int:
    today_utc = datetime.now(timezone.utc).date().isoformat()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM llm_call_log WHERE purpose = 'classifier' AND called_at >= ?",
        (today_utc,),
    ).fetchone()
    return int(row["n"]) if row else 0


def status_snapshot(conn: sqlite3.Connection, hours: int = 24) -> dict:
    """Aggregate metrics for the /status command and CLI diagnostics."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    today_utc = datetime.now(timezone.utc).date().isoformat()

    snap = {
        "items_total":     conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"],
        "items_pending":   conn.execute("SELECT COUNT(*) AS n FROM items WHERE classified_at IS NULL AND prefilter_passed = 1").fetchone()["n"],
        "items_classified_window": conn.execute(
            "SELECT COUNT(*) AS n FROM items WHERE classified_at >= ? AND classifier_error IS NULL",
            (cutoff,)
        ).fetchone()["n"],
        "classifier_errors_window": conn.execute(
            "SELECT COUNT(*) AS n FROM items WHERE classified_at >= ? AND classifier_error IS NOT NULL",
            (cutoff,)
        ).fetchone()["n"],
        "llm_calls_today": conn.execute(
            "SELECT COUNT(*) AS n FROM llm_call_log WHERE called_at >= ?",
            (today_utc,)
        ).fetchone()["n"],
        "llm_avg_ms_today": conn.execute(
            "SELECT AVG(duration_ms) AS d FROM llm_call_log WHERE called_at >= ? AND success = 1",
            (today_utc,)
        ).fetchone()["d"],
        "last_digest_at": (
            conn.execute("SELECT composed_at FROM digests ORDER BY composed_at DESC LIMIT 1").fetchone() or {}
        ).get("composed_at") if False else None,
        "enabled_users": conn.execute("SELECT COUNT(*) AS n FROM users WHERE enabled = 1").fetchone()["n"],
    }
    # The dict-or-row trick above doesn't work with sqlite3.Row; do it properly:
    last = conn.execute("SELECT composed_at FROM digests ORDER BY composed_at DESC LIMIT 1").fetchone()
    snap["last_digest_at"] = last["composed_at"] if last else None

    # Top categories in window
    snap["top_categories"] = [
        (r["category"], r["n"])
        for r in conn.execute(
            """SELECT category, COUNT(*) AS n FROM items
               WHERE classified_at >= ? AND classifier_error IS NULL AND category != 'other'
               GROUP BY category ORDER BY n DESC LIMIT 6""",
            (cutoff,)
        )
    ]

    # Top entities (parse entities_json — small enough to do in Python)
    import json as _json
    from collections import Counter
    ent_counter: Counter[str] = Counter()
    for r in conn.execute(
        """SELECT entities_json FROM items
           WHERE classified_at >= ? AND classifier_error IS NULL AND entities_json IS NOT NULL""",
        (cutoff,)
    ):
        try:
            for e in _json.loads(r["entities_json"]) or []:
                name = (e.get("name") if isinstance(e, dict) else str(e)) or ""
                if name:
                    ent_counter[name] += 1
        except Exception:
            continue
    snap["top_entities"] = ent_counter.most_common(8)

    # Migration-004 counts: graduation signals + commitments + meetings in window
    snap["graduation_signal_window"] = conn.execute(
        "SELECT COUNT(*) AS n FROM items WHERE classified_at >= ? AND graduation_signal = 1",
        (cutoff,)
    ).fetchone()["n"]
    snap["commitments_window"] = conn.execute(
        """SELECT COUNT(*) AS n FROM financial_commitments
           WHERE COALESCE(announced_at, created_at) >= ?""",
        (cutoff,)
    ).fetchone()["n"]
    snap["meetings_window"] = conn.execute(
        """SELECT COUNT(*) AS n FROM bilateral_meetings
           WHERE COALESCE(when_iso, created_at) >= ?""",
        (cutoff,)
    ).fetchone()["n"]
    return snap


def count_reviewed_24h(conn, as_of: datetime | None = None) -> int:
    """Total items processed (ingested) in the 24h window before `as_of`."""
    from datetime import timedelta, timezone
    import logging
    log = logging.getLogger("dalila.db")
    if as_of is None:
        as_of = datetime.now(timezone.utc)
    # Ensure UTC for reliable SQLite string comparison
    as_of_utc = as_of.astimezone(timezone.utc)
    since_utc = as_of_utc - timedelta(hours=24)
    
    q_params = (since_utc.isoformat(), as_of_utc.isoformat())
    # Return count of items ingested in this window.
    row = conn.execute(
        """SELECT COUNT(*) FROM items
           WHERE ingested_at >= ? AND ingested_at <= ?""",
        q_params
    ).fetchone()
    count = row[0] if row else 0
    log.info("count_reviewed_24h: window %s to %s -> %d", q_params[0], q_params[1], count)
    return count
