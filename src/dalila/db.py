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
    """Apply schema migrations and seed the sources table from sources.yaml."""
    cfg = get_config()
    schema = (cfg.migrations_dir / "001_initial.sql").read_text(encoding="utf-8")
    with connect() as conn:
        conn.executescript(schema)
        _seed_sources(conn)


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
    try:
        cur = conn.execute(
            """
            INSERT INTO items(
                source_id, url, url_hash, title, body, author,
                published_at, ingested_at, prefilter_passed
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
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
