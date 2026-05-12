"""Smoke tests — verify the scaffolding holds together without external deps."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch, tmp_path):
    """Each test gets its own DB. Reset the @lru_cache on get_config first."""
    monkeypatch.setenv("DALILA_DB_PATH", str(tmp_path / "test.db"))
    from dalila import config
    config.get_config.cache_clear()
    config.load_sources.cache_clear()
    config.load_prefilter_keywords.cache_clear()
    config.load_entities_yaml_text.cache_clear()
    config.load_entity_aliases.cache_clear()


def test_init_db_creates_schema():
    from dalila import db
    db.init_db()
    with db.connect() as conn:
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    assert {"sources", "items", "users", "digests", "deliveries", "llm_call_log"}.issubset(tables)


def test_init_db_seeds_sources():
    from dalila import db
    db.init_db()
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"]
    assert n > 5, f"expected sources to be seeded from sources.yaml; got {n}"


def test_url_hash_dedup_normalises_tracking_params():
    from dalila.db import url_hash
    h1 = url_hash("https://example.com/article?utm_source=foo&id=1")
    h2 = url_hash("https://example.com/article?utm_source=bar&campaign=x")
    assert h1 == h2, "tracking params should not break dedup"


def test_url_hash_distinguishes_different_articles():
    from dalila.db import url_hash
    assert url_hash("https://a.com/x") != url_hash("https://a.com/y")


def test_insert_item_dedup():
    from dalila import db
    from dalila.models import RawItem
    db.init_db()
    item = RawItem(source_id="wam_en", title="Test", url="https://example.com/a", body="b")
    with db.connect() as conn:
        first = db.insert_item(conn, item, prefilter_passed=True)
        second = db.insert_item(conn, item, prefilter_passed=True)
    assert first is not None
    assert second is None, "duplicate insert should return None"


def test_prefilter_matches_uae_keyword():
    from dalila.config import load_entity_aliases, load_prefilter_keywords
    from dalila.models import RawItem
    from dalila.pipeline import _prefilter_match
    kws = load_prefilter_keywords()
    aliases = load_entity_aliases()
    item = RawItem(
        source_id="x",
        title="UAE pledges $50m to Sudan response",
        url=None,
        body=None,
    )
    assert _prefilter_match(item, kws, aliases)


def test_prefilter_rejects_unrelated():
    from dalila.config import load_entity_aliases, load_prefilter_keywords
    from dalila.models import RawItem
    from dalila.pipeline import _prefilter_match
    kws = load_prefilter_keywords()
    aliases = load_entity_aliases()
    item = RawItem(source_id="x", title="Tech company X raises Series B", url=None, body=None)
    assert not _prefilter_match(item, kws, aliases)


def test_classification_from_dict():
    from dalila.models import Classification
    c = Classification.from_dict({
        "category": "humanitarian",
        "uae_relevance": 0.75,
        "severity": 0.8,
        "is_breaking_candidate": False,
        "entities": [{"name": "Sudan", "in_watchlist": True}],
        "doctrine_relation": None,
        "one_line_summary": "test",
        "rationale": "test",
    })
    assert c.category == "humanitarian"
    assert c.uae_relevance == 0.75


def test_llm_json_lenient_parser_strips_fences():
    from dalila.llm import _parse_json_lenient
    raw = "```json\n{\"a\": 1}\n```"
    assert _parse_json_lenient(raw) == {"a": 1}


def test_llm_json_lenient_parser_extracts_from_prose():
    from dalila.llm import _parse_json_lenient
    raw = "Here you go: {\"category\": \"humanitarian\"} hope that helps!"
    assert _parse_json_lenient(raw) == {"category": "humanitarian"}


def test_llm_json_lenient_parser_accepts_array():
    from dalila.llm import _parse_json_lenient
    raw = "```json\n[{\"a\": 1}, {\"a\": 2}]\n```"
    assert _parse_json_lenient(raw) == [{"a": 1}, {"a": 2}]


def test_classify_batch_extracts_items_array():
    """The batch extractor should accept both {items: [...]} and a bare array."""
    from dalila.classifier import _extract_items_array
    wrapped = {"items": [{"n": 2, "x": "b"}, {"n": 1, "x": "a"}]}
    out = _extract_items_array(wrapped, expected=2)
    assert [d["x"] for d in out] == ["a", "b"]  # sorted by n

    bare = [{"n": 1, "x": "a"}, {"n": 2, "x": "b"}]
    assert _extract_items_array(bare, expected=2) == bare


def test_classify_batch_size_mismatch_raises():
    from dalila.classifier import _extract_items_array
    import pytest
    with pytest.raises(ValueError, match="expected 3"):
        _extract_items_array({"items": [{"n": 1}, {"n": 2}]}, expected=3)


def test_build_batch_user_message_contains_all_items():
    from dalila.classifier import _build_batch_user_message
    msg = _build_batch_user_message([
        {"title": "First headline", "body": "Body one", "url": "https://a.com/1"},
        {"title": "Second headline", "body": "Body two", "url": "https://a.com/2"},
        {"title": "Third headline", "body": None, "url": None},
    ])
    assert "#1" in msg and "#2" in msg and "#3" in msg
    assert "First headline" in msg
    assert "Second headline" in msg
    assert "Third headline" in msg
    assert "https://a.com/1" in msg
    assert "EXACTLY" in msg  # the n-must-match-input directive


def test_entity_watchlist_is_substantial():
    """Watchlist needs ~4k+ tokens for Claude's prompt cache to actually engage.

    Rough rule of thumb: 1 token ≈ 4 chars. 4096 tokens ≈ 16KB.
    """
    from dalila.config import load_entities_yaml_text
    text = load_entities_yaml_text()
    assert len(text) > 12_000, (
        f"entities.yaml is {len(text)} chars; needs to be >12k for prompt caching to help. "
        "Expand the watchlist with more entities."
    )


def test_parse_rate_limit_reset_dubai_pm():
    """The exact phrasing seen in production: 'resets 5:30pm (Asia/Dubai)'."""
    from dalila.pipeline import parse_rate_limit_reset
    # Anchor 'now' to mid-morning Dubai time so 5:30pm is still in the future today.
    import pytz
    now_utc = pytz.timezone("Asia/Dubai").localize(datetime(2026, 5, 12, 9, 0)).astimezone(timezone.utc)
    reset = parse_rate_limit_reset(
        "claude CLI exit 1: You've hit your limit — resets 5:30pm (Asia/Dubai)",
        now=now_utc,
    )
    assert reset is not None
    local = reset.astimezone(pytz.timezone("Asia/Dubai"))
    assert (local.hour, local.minute) == (17, 30)
    assert local.date() == datetime(2026, 5, 12).date()


def test_parse_rate_limit_reset_rolls_to_tomorrow():
    """If the reset clock time has already passed today, return tomorrow's slot."""
    from dalila.pipeline import parse_rate_limit_reset
    import pytz
    tz = pytz.timezone("Asia/Dubai")
    # 'Now' is 6pm Dubai; reset is 5:30pm — must roll forward 24h.
    now_utc = tz.localize(datetime(2026, 5, 12, 18, 0)).astimezone(timezone.utc)
    reset = parse_rate_limit_reset("resets 5:30pm (Asia/Dubai)", now=now_utc)
    assert reset is not None
    local = reset.astimezone(tz)
    assert local.date() == datetime(2026, 5, 13).date()


def test_parse_rate_limit_reset_missing_returns_none():
    """A generic timeout error carries no reset time — caller should fall back."""
    from dalila.pipeline import parse_rate_limit_reset
    assert parse_rate_limit_reset("TimeoutError: connection reset") is None
