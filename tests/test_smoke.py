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
