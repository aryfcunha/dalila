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


def test_simhash_clusters_cross_outlet_reposts():
    """Same story across outlets should be much closer than two unrelated stories."""
    from dalila.simhash import simhash64, hamming
    a = simhash64("Reuters: Iran agrees ceasefire after talks in Geneva")
    b = simhash64("BBC News - Iran agrees ceasefire following Geneva talks")
    unrelated = simhash64("Drought in Horn of Africa worsens, UN warns of famine")
    assert hamming(a, b) <= 12, f"reposted-story distance was {hamming(a, b)}, must be ≤12"
    assert hamming(a, unrelated) >= 20, f"unrelated-story distance was {hamming(a, unrelated)}, must be ≥20"


def test_dedupe_by_simhash_drops_cross_outlet_repost():
    """In a sorted list, the second copy of a cross-outlet repost must be dropped."""
    from dalila.pipeline import _dedupe_by_simhash
    from dalila.simhash import simhash64, to_hex
    items = [
        {"id": 1, "title": "Reuters: Iran agrees ceasefire after talks in Geneva",
         "title_simhash": to_hex(simhash64("Reuters: Iran agrees ceasefire after talks in Geneva"))},
        {"id": 2, "title": "BBC News - Iran agrees ceasefire following Geneva talks",
         "title_simhash": to_hex(simhash64("BBC News - Iran agrees ceasefire following Geneva talks"))},
        {"id": 3, "title": "Drought in Horn of Africa worsens",
         "title_simhash": to_hex(simhash64("Drought in Horn of Africa worsens"))},
    ]
    kept_ids = [it["id"] for it in _dedupe_by_simhash(items)]
    assert kept_ids == [1, 3], f"expected [1, 3] (cross-outlet repost dropped), got {kept_ids}"


def test_doctrine_validate_rejects_bad_slug():
    """Topic slugs must be kebab-case starting with a letter."""
    from dalila.doctrine import _validate_action
    bad = {"action": "new", "topic": "Climate Finance!", "position_summary": "UAE supports private-sector-led climate finance."}
    action, _ = _validate_action(bad)
    assert action == "noop", "bad slug must downgrade to noop"


def test_doctrine_validate_accepts_well_formed():
    from dalila.doctrine import _validate_action
    good = {
        "action": "new",
        "topic": "climate-finance",
        "position_summary": "UAE backs private-sector-led climate finance models.",
        "nuance": "Subject to bilateral framing.",
        "confidence_delta": 0.05,
    }
    action, norm = _validate_action(good)
    assert action == "new"
    assert norm["topic"] == "climate-finance"
    assert abs(norm["confidence_delta"] - 0.05) < 1e-9


def test_doctrine_validate_clamps_confidence_delta():
    from dalila.doctrine import _validate_action
    p = {"action": "append", "topic": "conditionality",
         "position_summary": "UAE supports needs-based humanitarian aid.",
         "evolution_entry": {"relation": "reinforcing", "summary": "Restated by MoFAIC."},
         "confidence_delta": 5.0}
    action, norm = _validate_action(p)
    assert action == "append"
    assert norm["confidence_delta"] == 0.2, "delta must clamp to +/- 0.2"


def test_doctrine_fact_lifecycle_new_then_append(tmp_path, monkeypatch):
    """Insert a new doctrine fact, then append to it, and verify the log + confidence shift."""
    from dalila import db
    monkeypatch.setenv("DALILA_DB_PATH", str(tmp_path / "doctrine.db"))
    from dalila import config
    config.get_config.cache_clear()
    config.load_sources.cache_clear()
    db.init_db()
    with db.connect() as conn:
        # Need a real source + item for the FK
        conn.execute("INSERT OR IGNORE INTO sources(id,name,kind,url,quality,enabled) VALUES('t','t','rss','x',5,1)")
        conn.execute("INSERT INTO items(source_id,title,ingested_at,prefilter_passed) VALUES('t','i1', '2026-05-12T00:00:00+00:00', 1)")
        conn.execute("INSERT INTO items(source_id,title,ingested_at,prefilter_passed) VALUES('t','i2', '2026-05-12T00:00:00+00:00', 1)")
        i1 = conn.execute("SELECT id FROM items WHERE title='i1'").fetchone()["id"]
        i2 = conn.execute("SELECT id FROM items WHERE title='i2'").fetchone()["id"]
        db.upsert_doctrine_fact_new(
            conn, topic="climate-finance",
            position_summary="UAE backs private-sector-led climate finance.",
            nuance=None, source_item_id=i1,
        )
        db.append_doctrine_entry(
            conn, topic="climate-finance",
            position_summary="UAE backs private-sector-led climate finance, including blended models.",
            nuance="Now explicitly includes blended finance.",
            evolution_entry={"relation": "refining", "summary": "President's Bonn speech added blended-finance language."},
            source_item_id=i2, confidence_delta=0.1,
        )
        facts = db.list_doctrine_facts(conn)
    assert len(facts) == 1
    f = facts[0]
    assert f["topic"] == "climate-finance"
    assert len(f["evolution_log"]) == 2
    assert f["source_item_ids"] == [i1, i2]
    assert abs(f["confidence"] - 0.6) < 1e-9, f"expected 0.5 + 0.1 = 0.6, got {f['confidence']}"


# ── DeepSeek automatic fallback (Claude quota/rate-limit redundancy) ─────────

def _completed(returncode, stdout="", stderr=""):
    from types import SimpleNamespace
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _patch_llm(monkeypatch, *, cli_result=None, cli_exc=None,
               key="ds-key", fallback=None, backend=None, cooldown=None):
    """Wire llm so no real CLI / DB / network is touched. Drives behaviour
    through env vars (matching the real env-driven implementation)."""
    import contextlib
    from dalila import llm

    # Env knobs
    if key is None:
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    else:
        monkeypatch.setenv("DEEPSEEK_API_KEY", key)
    if fallback is None:
        monkeypatch.delenv("DALILA_DEEPSEEK_FALLBACK", raising=False)
    else:
        monkeypatch.setenv("DALILA_DEEPSEEK_FALLBACK", fallback)
    if backend is None:
        monkeypatch.delenv("DALILA_LLM_BACKEND", raising=False)
    else:
        monkeypatch.setenv("DALILA_LLM_BACKEND", backend)
    if cooldown is not None:
        monkeypatch.setenv("DALILA_DEEPSEEK_FALLBACK_COOLDOWN_MINUTES", cooldown)

    # Stub get_config so its load_dotenv() can't repopulate env vars we just
    # cleared (the dev .env may carry a real DEEPSEEK_API_KEY).
    from types import SimpleNamespace
    monkeypatch.setattr(llm, "get_config", lambda: SimpleNamespace(claude_bin="claude"))
    monkeypatch.setattr(llm, "_resolve_claude_bin", lambda: "claude")
    monkeypatch.setattr(llm, "_claude_cooldown_until", 0.0, raising=False)

    calls = {"deepseek": 0, "cli": 0}

    def fake_run(*a, **k):
        calls["cli"] += 1
        if cli_exc is not None:
            raise cli_exc
        return cli_result
    monkeypatch.setattr(llm.subprocess, "run", fake_run)

    def fake_deepseek(model, sysp, userp, purpose, timeout):
        calls["deepseek"] += 1
        return llm.LLMResponse(text="DEEPSEEK_OUTPUT", duration_ms=1)
    monkeypatch.setattr(llm, "_call_deepseek", fake_deepseek)

    monkeypatch.setattr(llm, "connect", lambda: contextlib.nullcontext(None))
    monkeypatch.setattr(llm, "record_llm_call", lambda *a, **k: None)
    return llm, calls


def test_is_capacity_error_classification():
    from dalila.llm import _is_capacity_error
    assert _is_capacity_error("You've hit your limit — resets 5:30pm (Asia/Dubai)")
    assert _is_capacity_error("Error: 429 Too Many Requests")
    assert _is_capacity_error("rate_limit_error: quota exceeded")
    assert _is_capacity_error("Service overloaded (529)")
    assert not _is_capacity_error("401 Unauthorized: invalid API key")
    assert not _is_capacity_error("invalid model name")


def test_quota_error_auto_falls_back(monkeypatch):
    result = _completed(1, stderr="You've hit your limit — resets 5:30pm (Asia/Dubai)")
    llm, calls = _patch_llm(monkeypatch, cli_result=result)  # fallback defaults ON
    out = llm.call(model=llm.HAIKU, system_prompt="s", user_prompt="u", purpose="editor")
    assert out == "DEEPSEEK_OUTPUT"
    assert calls["deepseek"] == 1


def test_missing_binary_falls_back(monkeypatch):
    llm, calls = _patch_llm(monkeypatch, cli_exc=FileNotFoundError("claude"))
    out = llm.call(model=llm.HAIKU, system_prompt="s", user_prompt="u", purpose="classify")
    assert out == "DEEPSEEK_OUTPUT"
    assert calls["deepseek"] == 1


def test_auth_error_does_not_fall_back(monkeypatch):
    from dalila.llm import LLMError
    result = _completed(1, stderr="401 Unauthorized: invalid API key")
    llm, calls = _patch_llm(monkeypatch, cli_result=result)
    with pytest.raises(LLMError):
        llm.call(model=llm.HAIKU, system_prompt="s", user_prompt="u", purpose="editor")
    assert calls["deepseek"] == 0


def test_fallback_disabled_raises_on_quota(monkeypatch):
    from dalila.llm import LLMError
    result = _completed(1, stderr="You've hit your limit, resets at 17:30 (UTC)")
    llm, calls = _patch_llm(monkeypatch, cli_result=result, fallback="0")
    with pytest.raises(LLMError):
        llm.call(model=llm.HAIKU, system_prompt="s", user_prompt="u", purpose="editor")
    assert calls["deepseek"] == 0


def test_no_key_means_no_fallback(monkeypatch):
    from dalila.llm import LLMError
    result = _completed(1, stderr="rate limit reached")
    llm, calls = _patch_llm(monkeypatch, cli_result=result, key=None)
    with pytest.raises(LLMError):
        llm.call(model=llm.HAIKU, system_prompt="s", user_prompt="u", purpose="editor")
    assert calls["deepseek"] == 0


def test_manual_backend_override_skips_cli(monkeypatch):
    llm, calls = _patch_llm(monkeypatch, backend="deepseek")
    out = llm.call(model=llm.HAIKU, system_prompt="s", user_prompt="u", purpose="editor")
    assert out == "DEEPSEEK_OUTPUT"
    assert calls["deepseek"] == 1
    assert calls["cli"] == 0   # CLI never spawned


def test_cooldown_routes_straight_to_deepseek(monkeypatch):
    result = _completed(1, stderr="rate limit reached")
    llm, calls = _patch_llm(monkeypatch, cli_result=result, cooldown="30")
    # First call trips the cooldown via a real CLI spawn + fallback.
    llm.call(model=llm.HAIKU, system_prompt="s", user_prompt="u", purpose="editor")
    assert calls["cli"] == 1 and calls["deepseek"] == 1
    # Second call must skip the CLI entirely.
    def boom(*a, **k):
        raise AssertionError("CLI should not be spawned during cooldown")
    monkeypatch.setattr(llm.subprocess, "run", boom)
    out = llm.call(model=llm.HAIKU, system_prompt="s", user_prompt="u", purpose="classify")
    assert out == "DEEPSEEK_OUTPUT"
    assert calls["deepseek"] == 2


# ── Markets: per-window deltas must not collapse onto a stale baseline ────────

def _pm_seed(conn, *, mid, source="manifold", prob_now, history):
    """history = list of (age_timedelta_kwargs, probability). Inserts a current
    snapshot plus the given historical rows."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    conn.execute(
        """INSERT INTO prediction_market_snapshots
               (market_id, source, question, probability, volume, url, topic_tags, recorded_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (mid, source, f"Will {mid}?", prob_now, 1000.0, f"https://x/{mid}", None, now.isoformat()),
    )
    for age, prob in history:
        conn.execute(
            """INSERT INTO prediction_market_history
                   (market_id, source, probability, delta_1h, delta_24h, recorded_at)
               VALUES (?,?,?,?,?,?)""",
            (mid, source, prob, None, None, (now - timedelta(**age)).isoformat()),
        )


def test_market_deltas_ignore_ancient_baseline():
    """A market whose only history is a pre-gap snapshot must NOT report three
    identical deltas — every window should be None (rendered as '--')."""
    from dalila import db
    from dalila.ingestors.prediction_markets import get_market_signals
    db.init_db()
    with db.connect() as conn:
        _pm_seed(conn, mid="stale", prob_now=0.20, history=[({"days": 40}, 0.80)])
        conn.commit()
        sig = {m["market_id"]: m for m in get_market_signals(conn, top_n=30)}
    m = sig["stale"]
    assert m["delta_30m"] is None
    assert m["delta_24h"] is None
    assert m["delta_7d"] is None


def test_market_deltas_distinct_per_window():
    """With a real row in each window's bracket, the three deltas differ and are
    each measured against the correct-age baseline."""
    from dalila import db
    from dalila.ingestors.prediction_markets import get_market_signals
    db.init_db()
    with db.connect() as conn:
        _pm_seed(conn, mid="live", prob_now=0.50, history=[
            ({"minutes": 40}, 0.48),   # ~30m window  → +0.02
            ({"hours": 24},   0.40),   # ~24h window  → +0.10
            ({"days": 7},     0.30),   # ~1w window   → +0.20
        ])
        conn.commit()
        sig = {m["market_id"]: m for m in get_market_signals(conn, top_n=30)}
    m = sig["live"]
    assert abs(m["delta_30m"] - 0.02) < 1e-9
    assert abs(m["delta_24h"] - 0.10) < 1e-9
    assert abs(m["delta_7d"]  - 0.20) < 1e-9
    # The whole point: they are NOT all equal.
    assert len({round(m["delta_30m"],4), round(m["delta_24h"],4), round(m["delta_7d"],4)}) == 3


# ── Countries page: single-pass aggregator replaces the per-country fan-out ───

def _insert_classified_item(conn, *, iid, title, countries, published_at, source_id):
    import json as _json
    conn.execute(
        "INSERT INTO items (id, source_id, url, url_hash, title, published_at, "
        "ingested_at, prefilter_passed, classified_at, category, uae_relevance, "
        "severity, one_line_summary, country_focus_json) "
        "VALUES (?,?,?,?,?,?,?,1,?,?,?,?,?,?)",
        (iid, source_id, f"https://x/{iid}", f"hash{iid}", title, published_at,
         published_at, published_at, "humanitarian", 0.8, 0.5, "s",
         _json.dumps(countries)),
    )


def test_country_aggregates_matches_legacy_functions():
    """country_aggregates must produce, in ONE pass, exactly what the four
    legacy per-country functions produced in ~2N+2 passes."""
    from datetime import datetime, timezone, timedelta
    from dalila import db
    db.init_db()
    now = datetime.now(timezone.utc)
    def ts(days_ago): return (now - timedelta(days=days_ago)).isoformat()
    with db.connect() as conn:
        src = conn.execute("SELECT id FROM sources LIMIT 1").fetchone()["id"]
        _insert_classified_item(conn, iid=1, title="A", countries=["SD", "AE"], published_at=ts(1), source_id=src)
        _insert_classified_item(conn, iid=2, title="B", countries=["SD", "IR"], published_at=ts(2), source_id=src)
        _insert_classified_item(conn, iid=3, title="C", countries=["AE"],       published_at=ts(3), source_id=src)
        conn.commit()

        agg = db.country_aggregates(conn, since_hours=24 * 180, items_limit=30)

        assert agg["counts"] == db.country_mention_counts(conn, since_hours=24 * 180)
        assert agg["timeline"] == db.country_timeline(conn, since_hours=24 * 180)
        for iso in agg["counts"]:
            assert agg["cooccurrence"].get(iso, {}) == db.country_cooccurrence(conn, iso, since_hours=24 * 180)
            legacy_ids = [i["id"] for i in db.items_for_country(conn, iso, since_hours=24 * 180, limit=30)]
            new_ids = [i["id"] for i in agg["items_by_country"].get(iso, [])]
            assert new_ids == legacy_ids, f"item list mismatch for {iso}"

    # sanity on the actual aggregates
    assert agg["counts"] == {"SD": 2, "AE": 2, "IR": 1}
    assert agg["cooccurrence"]["SD"] == {"AE": 1, "IR": 1}
    # newest-first within a country (item 1 is more recent than item 2 for SD)
    assert [i["id"] for i in agg["items_by_country"]["SD"]] == [1, 2]


def test_country_aggregates_dedupes_within_row_and_drops_junk():
    """Stricter-than-legacy validation: a row listing a country twice can't
    double-count, and non-alpha / wrong-length codes are dropped."""
    from datetime import datetime, timezone
    from dalila import db
    db.init_db()
    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        src = conn.execute("SELECT id FROM sources LIMIT 1").fetchone()["id"]
        _insert_classified_item(conn, iid=1, title="dupes", countries=["SD", "SD", "AE", "99", "ABC", ""], published_at=now, source_id=src)
        conn.commit()
        agg = db.country_aggregates(conn, since_hours=24 * 180)
    assert agg["counts"] == {"SD": 1, "AE": 1}              # SD counted once; junk dropped
    assert agg["cooccurrence"]["SD"] == {"AE": 1}           # no SD->SD self-pair


# ── #3 country page excludes category='other' junk ───────────────────────────

def test_country_page_excludes_other_category_junk():
    """Sports/trivia (classifier -> category='other') must NOT appear on the
    country page, even when tagged to a country."""
    from datetime import datetime, timezone
    from dalila import db
    db.init_db()
    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        src = conn.execute("SELECT id FROM sources LIMIT 1").fetchone()["id"]
        # legit dev item + junk 'other' item, both tagged PT
        _insert_classified_item(conn, iid=1, title="Portugal pledges aid to Sudan", countries=["PT", "SD"], published_at=now, source_id=src)
        conn.execute(
            "INSERT INTO items (id, source_id, url, url_hash, title, published_at, "
            "ingested_at, prefilter_passed, classified_at, category, uae_relevance, "
            "severity, one_line_summary, country_focus_json) "
            "VALUES (2,?,?,?,?,?,?,1,?,'other',1.0,0.1,'s','[\"PT\"]')",
            (src, "https://x/2", "h2", "Drone Show in Matosinhos Breaks Guinness Record", now, now, now),
        )
        conn.commit()
        agg = db.country_aggregates(conn, since_hours=24 * 180)
    assert agg["counts"].get("PT") == 1, "only the legit dev item should count for PT"
    pt_ids = [i["id"] for i in agg["items_by_country"].get("PT", [])]
    assert pt_ids == [1], "the 'other' junk item must be excluded from the country page"


# ── #7 bilateral meetings de-duplicate at render time ────────────────────────

def _insert_meeting(conn, *, mid, uae, foreign, country, when_iso, item_id, title, source_id):
    from datetime import datetime, timezone
    created = datetime.now(timezone.utc).isoformat()  # always set (NOT NULL)
    conn.execute(
        "INSERT OR IGNORE INTO items (id, source_id, url, url_hash, title, ingested_at, prefilter_passed) "
        "VALUES (?,?,?,?,?,?,1)",
        (item_id, source_id, f"https://x/{item_id}", f"mh{item_id}", title, created),
    )
    conn.execute(
        "INSERT INTO bilateral_meetings (id, item_id, uae_principal, foreign_principal, "
        "foreign_country, meeting_type, when_iso, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (mid, item_id, uae, foreign, country, "meeting", when_iso, created),
    )


def test_bilateral_meetings_dedupe_collapses_duplicates():
    from datetime import datetime, timezone
    from dalila import db
    db.init_db()
    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        src = conn.execute("SELECT id FROM sources LIMIT 1").fetchone()["id"]
        # same meeting reported by 3 outlets (punctuation/case noise), same date
        _insert_meeting(conn, mid=1, uae="Mohamed bin Zayed Al Nahyan", foreign="Marco Rubio", country="US", when_iso="2026-06-24", item_id=101, title="A", source_id=src)
        _insert_meeting(conn, mid=2, uae="mohamed bin zayed al nahyan", foreign="Marco  Rubio.", country="US", when_iso="2026-06-24", item_id=102, title="B", source_id=src)
        _insert_meeting(conn, mid=3, uae="Mohamed bin Zayed Al-Nahyan", foreign="Marco Rubio", country="US", when_iso=None, item_id=103, title="C", source_id=src)
        # a genuinely different meeting
        _insert_meeting(conn, mid=4, uae="Mohamed bin Zayed Al Nahyan", foreign="Emmanuel Macron", country="FR", when_iso="2026-06-24", item_id=104, title="D", source_id=src)
        # two blank-both rows must stay distinct
        _insert_meeting(conn, mid=5, uae=None, foreign=None, country=None, when_iso="2026-06-24", item_id=105, title="E", source_id=src)
        _insert_meeting(conn, mid=6, uae=None, foreign=None, country=None, when_iso="2026-06-24", item_id=106, title="F", source_id=src)
        conn.commit()
        meetings = db.recent_bilateral_meetings(conn, hours=24 * 60, limit=30)

    keyed = {(m.get("uae_principal"), m.get("foreign_principal")): m for m in meetings}
    rubio = next(m for m in meetings if (m.get("foreign_principal") or "").startswith("Marco"))
    assert rubio["mention_count"] == 3, "the 3 Rubio reports (incl. null-date) should collapse to one"
    assert len(rubio["sources"]) == 3
    # Macron meeting separate; two blank-both rows stay separate => 1 + 1 + 2 = 4 entries
    blanks = [m for m in meetings if not m.get("uae_principal") and not m.get("foreign_principal")]
    assert len(blanks) == 2, "blank-both meetings must NOT be merged"
    assert len(meetings) == 4
# ── Batch 2: source re-wiring (WAM sitemap, MoFA scrape, skip instrumentation) ─

def test_sitemap_fetch_reads_news_title_and_filters_old(monkeypatch):
    """WAM-style Google-News sitemap: headline from <news:title>, date from
    <news:publication_date>, entries older than recent_hours dropped."""
    from datetime import datetime, timezone, timedelta
    from dalila.ingestors import sitemap
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(hours=1)).isoformat()
    old = (now - timedelta(days=10)).isoformat()
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
        'xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">'
        '<url><loc>https://www.wam.ae/en/article/recent</loc>'
        f'<lastmod>{recent}</lastmod>'
        f'<news:news><news:publication_date>{recent}</news:publication_date>'
        '<news:title>UAE announces aid package for Sudan</news:title></news:news></url>'
        '<url><loc>https://www.wam.ae/en/article/old</loc>'
        f'<news:news><news:publication_date>{old}</news:publication_date>'
        '<news:title>Old headline</news:title></news:news></url>'
        '</urlset>'
    ).encode("utf-8")
    monkeypatch.setattr(sitemap, "_http_get", lambda url, **k: xml)
    items = sitemap.fetch({
        "id": "wam_en",
        "url": "https://www.wam.ae/sitemap/news/en/{year}/{month}/english.xml",
        "recent_hours": 48,
    })
    titles = [it.title for it in items]
    assert "UAE announces aid package for Sudan" in titles
    assert "Old headline" not in titles  # filtered by recent_hours
    assert items[0].url == "https://www.wam.ae/en/article/recent"
    assert items[0].source_id == "wam_en"


def test_scrape_selects_anchor_cards_directly(monkeypatch):
    """MoFA card: headline in <h2>, URL in a separate 'View Details' link.
    Title must come from the heading (not the generic link text)."""
    from dalila.ingestors import scrape
    html = (
        "<html><body>"
        "<div class='card-content'>"
        "  <h2 class='line-clamp-3'>UAE and Venezuela discuss cooperation</h2>"
        "  <a href='/en/MediaHub/News/2026/6/25/UAE-Venezuela'>View Details</a>"
        "</div>"
        "<div class='card-content'>"
        "  <h2 class='line-clamp-3'>Abdullah bin Zayed receives British counterpart</h2>"
        "  <a href='/en/MediaHub/News/2026/6/25/UAE-UK'>View Details</a>"
        "</div>"
        "</body></html>"
    )

    class _Resp:
        text = html
        def raise_for_status(self):
            pass

    monkeypatch.setattr(scrape.httpx, "get", lambda *a, **k: _Resp())
    items = scrape.fetch({
        "id": "mofa_uae",
        "url": "https://www.mofa.gov.ae/en/mediahub/news",
        "selector": ".card-content",
    })
    titles = [it.title for it in items]
    assert any("Venezuela" in t for t in titles)
    # title is the headline, NOT the "View Details" CTA text
    assert "View Details" not in titles
    assert all("MediaHub/News/2026" in it.url for it in items)
    assert len(items) == 2


def test_run_ingest_records_skipped_status(monkeypatch):
    """A keyless creds-gated ingestor surfaces as last_error='skipped: ...',
    not as a healthy 0-item poll."""
    from dalila import db
    from dalila.ingestors.base import SourceSkipped
    from dalila import pipeline
    db.init_db()
    monkeypatch.setattr(pipeline, "iter_enabled_sources",
                        lambda: iter([{"id": "acled", "kind": "acled", "tags": []}]))

    def _skip(src):
        raise SourceSkipped("ACLED_USERNAME / ACLED_PASSWORD not set")

    monkeypatch.setattr(pipeline, "ingest_source", _skip)
    stats = pipeline.run_ingest()
    assert stats["acled"]["error"].startswith("skipped:")
    assert "ACLED" in stats["acled"]["error"]
# ── #6 semantic (same-event) news dedup ──────────────────────────────────────

def test_dedupe_semantic_collapses_same_event_clusters(monkeypatch):
    from dalila import db, pipeline
    db.init_db()
    items = [
        {"id": 10, "title": "UN denounces genocide in Gaza"},
        {"id": 11, "title": "Floods displace thousands in Sudan"},
        {"id": 12, "title": "UN experts repeat Gaza genocide claim"},  # same event as #10
        {"id": 13, "title": "OPEC weighs output cut"},
    ]
    # LLM says positions 1 & 3 (ids 10, 12) are the same event.
    monkeypatch.setattr(pipeline, "_llm_cluster_same_event", lambda cands: [[1, 3]])
    with db.connect() as conn:
        out = pipeline._dedupe_semantic(conn, items)
    assert [it["id"] for it in out] == [10, 11, 13]  # keep first of cluster (10), drop 12


def test_dedupe_semantic_noops_on_llm_failure(monkeypatch):
    from dalila import db, pipeline
    db.init_db()
    items = [{"id": 1, "title": "X headline"}, {"id": 2, "title": "Y headline"}]

    def _boom(cands):
        raise RuntimeError("rate limit reached")

    monkeypatch.setattr(pipeline, "_llm_cluster_same_event", _boom)
    with db.connect() as conn:
        out = pipeline._dedupe_semantic(conn, items)
    assert [it["id"] for it in out] == [1, 2]  # unchanged — never makes the brief worse


def test_llm_cluster_parsing_filters_singletons_and_out_of_range(monkeypatch):
    from dalila import llm, pipeline
    monkeypatch.setattr(llm, "call_json",
                        lambda **k: {"clusters": [[1, 2], [3], [2, 99], [4, 5]]})
    items = [{"title": f"t{i}", "one_line_summary": ""} for i in range(6)]  # positions 1..6
    groups = pipeline._llm_cluster_same_event(items)
    assert [1, 2] in groups and [4, 5] in groups
    assert all(len(g) >= 2 for g in groups)          # singleton [3] dropped
    assert not any(99 in g for g in groups)          # out-of-range trimmed → [2] dropped
# ── #2 prediction-market history backfill (Manifold + Kalshi) ─────────────────

def test_kalshi_candle_prob_parsing():
    from dalila.ingestors.prediction_markets import _kalshi_candle_prob
    assert _kalshi_candle_prob({"price": {"close_dollars": "0.42"}}) == 0.42
    mid = _kalshi_candle_prob({"price": {}, "yes_bid": {"close_dollars": "0.30"},
                               "yes_ask": {"close_dollars": "0.40"}})
    assert abs(mid - 0.35) < 1e-9
    assert _kalshi_candle_prob({"price": {}}) is None          # nothing usable


def test_backfill_manifold_parsing_stops_at_window(monkeypatch):
    import time
    from dalila.ingestors import prediction_markets as pm
    now_ms = int(time.time() * 1000)
    bets = [
        {"id": "b3", "probAfter": 0.5, "createdTime": now_ms - 3_600_000},        # in window
        {"id": "b2", "probAfter": 0.4, "createdTime": now_ms - 7_200_000},        # in window
        {"id": "b1", "probAfter": 0.3, "createdTime": now_ms - 10 * 86_400_000},  # older → stop
    ]

    def fake_get(url, timeout=10):
        if "/slug/" in url:
            return {"id": "cid1"}
        if "/bets" in url:
            return bets
        return {}

    monkeypatch.setattr(pm, "_http_get", fake_get)
    out = pm._backfill_manifold_market("some-slug", now_ms - 7 * 86_400 * 1000)
    assert [p for p, _ in out] == [0.5, 0.4]   # older-than-window bet excluded


def test_backfill_market_history_idempotent(monkeypatch):
    from dalila import db
    from dalila.ingestors import prediction_markets as pm
    db.init_db()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO prediction_market_snapshots "
            "(market_id, source, question, probability, recorded_at) VALUES (?,?,?,?,?)",
            ("m1", "manifold", "Will X?", 0.5, db._now_iso()),
        )
        conn.commit()
        monkeypatch.setattr(pm, "_backfill_manifold_market",
                            lambda slug, since_ms: [(0.5, "2026-06-20T00:00:00+00:00"),
                                                    (0.6, "2026-06-21T00:00:00+00:00")])
        s1 = pm.backfill_market_history(conn, days=14)
        s2 = pm.backfill_market_history(conn, days=14)
        n = conn.execute("SELECT COUNT(*) FROM prediction_market_history").fetchone()[0]
    assert s1["inserted"] == 2
    assert s2["inserted"] == 0   # re-run is idempotent
    assert n == 2
