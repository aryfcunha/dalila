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
            conn.executescript(sql_path.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO schema_migrations(filename, applied_at) VALUES(?, ?)",
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


def insert_item(conn: sqlite3.Connection, item: RawItem, prefilter_passed: bool) -> int | None:
    """Insert a new item; return the row id, or None if it was a duplicate."""
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
            classifier_error = NULL
        WHERE id = ?
        """,
        (
            _now_iso(),
            c.category,
            c.uae_relevance,
            c.severity,
            1 if c.is_breaking_candidate else 0,
            c.doctrine_relation,
            c.one_line_summary,
            c.rationale,
            json.dumps(c.entities, ensure_ascii=False),
            item_id,
        ),
    )


def save_classifier_error(conn: sqlite3.Connection, item_id: int, error: str) -> None:
    conn.execute(
        "UPDATE items SET classifier_error = ?, classified_at = ? WHERE id = ?",
        (error[:1000], _now_iso(), item_id),
    )


def items_for_digest(conn: sqlite3.Connection, since_hours: int = 24, min_relevance: float = 0.4) -> list[dict]:
    """Items classified in the last N hours above the relevance threshold."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
    rows = conn.execute(
        """
        SELECT i.id, i.title, i.url, i.body, i.one_line_summary, i.category,
               i.uae_relevance, i.severity, i.entities_json, i.doctrine_relation,
               i.title_simhash,
               s.name AS source_name, s.quality AS source_quality
        FROM items i
        JOIN sources s ON s.id = i.source_id
        WHERE i.classified_at IS NOT NULL
          AND i.classified_at >= ?
          AND COALESCE(i.uae_relevance, 0) >= ?
          AND i.category != 'other'
        ORDER BY (i.uae_relevance * COALESCE(i.severity, 0.5) * s.quality) DESC,
                 i.ingested_at DESC
        """,
        (cutoff, min_relevance),
    ).fetchall()
    out = []
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
        })
    return out


def save_digest(conn: sqlite3.Connection, date_label: str, content: str, item_ids: list[int]) -> int:
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


def get_url_for_item(conn: sqlite3.Connection, item_id: int) -> str | None:
    row = conn.execute("SELECT url FROM items WHERE id = ?", (item_id,)).fetchone()
    return row["url"] if row else None


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
    return snap
