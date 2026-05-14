"""Prediction markets ingestor — fully dynamic, digest-driven.

No static market watchlist. Markets are discovered by:
  1. Extracting top entities/topics from items that made it into the last 7
     days of digests (the actual news that mattered to the UAE lens).
  2. Searching Manifold for active binary markets on those entities.
  3. Falling back to domain seed terms (UAE, humanitarian, GCC, etc.) when
     digest history is sparse (new install, first few days).

Delta tracking
--------------
On every poll, 1h and 24h deltas are computed from the history table.
Large moves flag a market as a breaking-news signal so the scheduler can
trigger an urgent ingest + classify cycle.

Digest integration
------------------
`get_market_signals(conn, digest_items)` ranks markets by:
  composite = 2 * abs(delta_24h) + topic_overlap_with_today's_digest
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# ── constants ──────────────────────────────────────────────────────────────────
MANIFOLD_BASE = "https://api.manifold.markets/v0"
KALSHI_BASE   = "https://external-api.kalshi.com/trade-api/v2"
SETTINGS_PATH = Path(__file__).parents[3] / "prediction_markets.yaml"

_UA = {"User-Agent": "Dalila/0.1 (UAE intelligence digest; +https://github.com/aryfcunha/dalila)"}

# Domain fallback seeds — used when digest history is sparse.
_DOMAIN_SEEDS = [
    "UAE foreign policy",
    "humanitarian crisis 2026",
    "Gaza ceasefire",
    "Iran nuclear",
    "Strait of Hormuz",
    "global recession 2026",
    "Sudan famine",
    "GCC economy",
]


# ── settings loader ────────────────────────────────────────────────────────────
def _settings() -> dict:
    try:
        import yaml
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _s(key_path: str, default):
    """Dot-path lookup into settings dict, e.g. 'discovery.min_volume'."""
    cfg = _settings()
    for k in key_path.split("."):
        if not isinstance(cfg, dict):
            return default
        cfg = cfg.get(k, default)
    return cfg if cfg is not None else default


# ── HTTP helper ────────────────────────────────────────────────────────────────
def _http_get(url: str, timeout: int = 10) -> dict | list:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


# ── Manifold API ───────────────────────────────────────────────────────────────
def _search_manifold(query: str, limit: int = 5) -> list[dict]:
    """Search Manifold for active binary markets. Returns list of market dicts."""
    try:
        url = (
            f"{MANIFOLD_BASE}/search-markets"
            f"?term={urllib.parse.quote(query)}"
            f"&limit={limit}&sort=liquidity&filter=open&contractType=BINARY"
        )
        data = _http_get(url, timeout=8)
        results = []
        min_vol = float(_s("discovery.min_volume", 300))
        for m in (data if isinstance(data, list) else []):
            question = m.get("question", "")
            prob = m.get("probability")
            vol  = float(m.get("volume", 0))
            
            if prob is None or vol < min_vol:
                continue
            
            # Filter out very long-term markets (e.g., 2035, 2050)
            years = re.findall(r"\b20[3-9][0-9]\b", question)
            if years:
                continue

            results.append({
                "source":      "manifold",
                "market_id":   m.get("slug") or m["id"],
                "question":    question,
                "probability": float(prob),
                "volume":      vol,
                "url":         m.get("url", ""),
            })
        return results
    except Exception as exc:
        log.debug("manifold search failed for %r: %s", query, exc)
        return []


def _refresh_manifold_prob(slug: str) -> tuple[float | None, float | None]:
    """Fetch latest (probability, volume) for a known Manifold market."""
    for url in [
        f"{MANIFOLD_BASE}/slug/{urllib.parse.quote(slug)}",
        f"{MANIFOLD_BASE}/market/{urllib.parse.quote(slug)}",
    ]:
        try:
            data = _http_get(url, timeout=8)
            prob = data.get("probability")
            vol  = data.get("volume")
            if prob is not None:
                return float(prob), vol
        except Exception:
            continue
    return None, None


# ── Kalshi API ─────────────────────────────────────────────────────────────────
def _refresh_kalshi_prob(ticker: str) -> tuple[float | None, float | None]:
    """Fetch latest (probability, volume) for a known Kalshi market/event ticker."""
    for url in [
        f"{KALSHI_BASE}/markets/{ticker.upper()}",
        f"{KALSHI_BASE}/events/{ticker.upper()}?with_nested_markets=true",
    ]:
        try:
            data = _http_get(url, timeout=8)
            market = data.get("market") or {}
            if not market:
                markets = data.get("event", {}).get("markets", [])
                market = markets[0] if markets else {}
            yb  = market.get("yes_bid")
            ya  = market.get("yes_ask")
            lp  = market.get("last_price")
            vol = market.get("volume")
            if yb is not None and ya is not None:
                return (yb + ya) / 200.0, vol
            if lp is not None:
                return lp / 100.0, vol
        except Exception:
            continue
    return None, None


# ── Entity extraction from digests ─────────────────────────────────────────────
def _recent_digest_entities(conn: sqlite3.Connection) -> list[str]:
    """Extract top entities/topics from items in the last N days' digests.

    Returns a ranked list of search terms — entity names first, then
    high-frequency keywords from titles and summaries.
    """
    days = int(_s("discovery.digest_lookback_days", 7))
    entity_limit = int(_s("discovery.entity_limit", 12))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    # 1. Gather item IDs from recent digests
    digest_rows = conn.execute(
        "SELECT item_ids_json FROM digests WHERE composed_at >= ?", (cutoff,)
    ).fetchall()
    item_ids: list[int] = []
    for row in digest_rows:
        try:
            item_ids.extend(int(x) for x in (json.loads(row[0] or "[]") or []))
        except Exception:
            continue

    if not item_ids:
        return []

    # 2. Pull entity lists + text from those items
    placeholders = ",".join("?" * len(item_ids))
    rows = conn.execute(
        f"SELECT entities_json, one_line_summary, title FROM items WHERE id IN ({placeholders})",
        item_ids,
    ).fetchall()

    freq: dict[str, float] = {}

    for row in rows:
        # Named entities (classifier output) — weighted higher
        try:
            for ent in (json.loads(row[0] or "[]") or []):
                name = (ent.get("name") or "").strip()
                if name and len(name) > 3:
                    freq[name] = freq.get(name, 0) + 3.0
        except Exception:
            pass
        # Keywords from title + summary
        text = f"{row[2] or ''} {row[1] or ''}".lower()
        for word in re.findall(r"\b[a-z][a-z]{3,}\b", text):
            if word not in {
                "that", "with", "this", "from", "have", "were", "will",
                "also", "said", "been", "their", "after", "about", "which",
            }:
                freq[word] = freq.get(word, 0) + 1.0

    ranked = sorted(freq.items(), key=lambda x: x[1], reverse=True)
    return [term for term, _ in ranked[:entity_limit]]


# ── Market discovery ──────────────────────────────────────────────────────────
def discover_markets(conn: sqlite3.Connection) -> list[dict]:
    """Build a fresh market set by searching Manifold for digest entities.

    Falls back to domain seeds when digest history is thin.
    Returns a deduplicated list of market dicts, ranked by volume.
    """
    entities = _recent_digest_entities(conn)

    # Fallback when no digest history yet
    if len(entities) < 3:
        log.info("prediction markets: sparse digest history — using domain seeds")
        entities = list(_DOMAIN_SEEDS)
    else:
        # Also append a few domain seeds as anchors so UAE/humanitarian lens
        # is never completely absent even when the digest is all geopolitics
        seeds = _s("discovery.domain_seeds", _DOMAIN_SEEDS)
        for seed in seeds[:3]:
            if seed not in entities:
                entities.append(seed)

    log.info("prediction markets: discovering markets for %d search terms", len(entities))

    max_markets = int(_s("discovery.max_markets", 25))
    seen: dict[str, dict] = {}  # market_id -> dict

    for term in entities[:10]:  # cap API calls
        for m in _search_manifold(term, limit=4):
            mid = m["market_id"]
            if mid not in seen:
                seen[mid] = m
            else:
                # A market appearing for multiple search terms is more relevant
                seen[mid]["_relevance_score"] = seen[mid].get("_relevance_score", 1) + 1

    # Rank: relevance_score first, then volume
    ranked = sorted(
        seen.values(),
        key=lambda x: (x.get("_relevance_score", 1), x.get("volume", 0)),
        reverse=True,
    )
    return ranked[:max_markets]


# ── DB helpers ─────────────────────────────────────────────────────────────────
def _prior_prob(conn: sqlite3.Connection, market_id: str,
                source: str, hours_ago: float) -> float | None:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    row = conn.execute(
        """SELECT probability FROM prediction_market_history
           WHERE market_id = ? AND source = ? AND recorded_at <= ?
           ORDER BY recorded_at DESC LIMIT 1""",
        (market_id, source, cutoff),
    ).fetchone()
    return float(row[0]) if row else None


def _upsert(conn: sqlite3.Connection, m: dict, prob: float,
            volume: float | None, d1h: float | None, d24h: float | None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO prediction_market_snapshots
               (market_id, source, question, probability, volume, topic_tags, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(market_id, source) DO UPDATE SET
               probability = excluded.probability,
               volume      = COALESCE(excluded.volume, prediction_market_snapshots.volume),
               question    = excluded.question,
               recorded_at = excluded.recorded_at""",
        (m["market_id"], m["source"], m.get("question", m["market_id"]),
         prob, volume, None, now),
    )
    conn.execute(
        """INSERT INTO prediction_market_history
               (market_id, source, probability, delta_1h, delta_24h, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (m["market_id"], m["source"], prob, d1h, d24h, now),
    )


# ── Main poll ─────────────────────────────────────────────────────────────────
def poll_markets(conn: sqlite3.Connection) -> list[dict]:
    """Discover and poll markets. Returns list of large-move alert dicts."""
    markets = discover_markets(conn)
    thresh_1h  = float(_s("alerts.threshold_1h",  0.05))
    thresh_24h = float(_s("alerts.threshold_24h", 0.08))
    alerts: list[dict] = []

    for m in markets:
        mid = m["market_id"]
        src = m["source"]

        # If discovery already fetched the probability, use it directly;
        # otherwise do a targeted refresh.
        prob   = m.get("probability")
        volume = m.get("volume")
        if prob is None:
            if src == "manifold":
                prob, volume = _refresh_manifold_prob(mid)
            elif src == "kalshi":
                prob, volume = _refresh_kalshi_prob(mid)

        if prob is None:
            continue

        d1h  = None
        d24h = None
        p1h  = _prior_prob(conn, mid, src, 1.0)
        p24h = _prior_prob(conn, mid, src, 24.0)
        if p1h  is not None: d1h  = prob - p1h
        if p24h is not None: d24h = prob - p24h

        _upsert(conn, m, prob, volume, d1h, d24h)

        if (d1h  is not None and abs(d1h)  >= thresh_1h) or \
           (d24h is not None and abs(d24h) >= thresh_24h):
            alerts.append({
                "market_id": mid, "source": src,
                "question":  m.get("question", mid),
                "probability": prob,
                "delta_1h": d1h, "delta_24h": d24h,
            })
            log.info(
                "MARKET ALERT %s: prob=%.1f%% d1h=%+.1f%% d24h=%+.1f%%",
                mid, prob * 100,
                (d1h or 0) * 100, (d24h or 0) * 100,
            )

    return alerts


# ── Scoring Helpers ────────────────────────────────────────────────────────────
def _logit(p: float) -> float:
    """Log-odds of p. Clamped to [0.001, 0.999] to avoid infinity."""
    p = max(0.001, min(0.999, p))
    return math.log(p / (1 - p))


def _topic_overlap(market: dict, digest_items: list[dict]) -> float:
    """Fraction of market question words found in today's digest content."""
    mq = (market.get("question") or "").lower()
    market_words = set(re.findall(r"\b[a-z]{5,}\b", mq)) - {
        "which", "could", "would", "their", "there", "after", "before", "will", "does"
    }
    if not market_words:
        return 0.0
    
    # Priority boosts for specific regional beats
    boost = 0.0
    beats = ["sudan", "opec", "hormuz", "drone", "uae", "saudi", "afghanistan", "sahel", "yemen"]
    for beat in beats:
        if beat in mq:
            boost += 0.3

    if not digest_items:
        return boost

    digest_text = " ".join(
        (it.get("title") or "") + " " + (it.get("summary") or "")
        for it in digest_items
    ).lower()
    digest_words = set(re.findall(r"\b[a-z]{5,}\b", digest_text))
    return (len(market_words & digest_words) / len(market_words)) + boost


def _is_duplicate_topic(m1: dict, m2: dict) -> bool:
    """True if two markets seem to cover the same question."""
    def _sig_words(q: str) -> set[str]:
        return set(re.findall(r"\b[a-z]{5,}\b", q.lower())) - {"manifold", "kalshi", "will", "does"}
    
    w1 = _sig_words(m1["question"])
    w2 = _sig_words(m2["question"])
    if not w1 or not w2: return False
    
    overlap = len(w1 & w2) / min(len(w1), len(w2))
    return overlap > 0.6


def get_market_signals(conn: sqlite3.Connection,
                       digest_items: list[dict] | None = None) -> list[dict]:
    """Return top N market signals scored for today's digest content."""
    top_n = int(_s("digest.top_n", 5))

    rows = conn.execute(
        """SELECT s.market_id, s.source, s.question, s.probability, s.volume,
                  h.delta_24h, h.probability as p_old
           FROM prediction_market_snapshots s
           LEFT JOIN (
               SELECT market_id, source, probability, delta_24h
               FROM prediction_market_history
               WHERE recorded_at <= datetime('now', '-23 hours')
               GROUP BY market_id, source
               HAVING recorded_at = MAX(recorded_at)
           ) h ON h.market_id = s.market_id AND h.source = s.source"""
    ).fetchall()

    scored = []
    for row in rows:
        m = {
            "market_id":   row[0], "source":      row[1],
            "question":    row[2], "probability": row[3],
            "volume":      row[4], "delta_24h":   row[5],
        }
        p_new = m["probability"]
        p_old = row[6]
        
        # Scoring: Log-odds shift captures 1% -> 10% movements better than absolute p.p.
        if p_old is not None:
            # Shift = |logit(new) - logit(old)|
            shift = abs(_logit(p_new) - _logit(p_old))
        else:
            shift = 0.0
            
        overlap = _topic_overlap(m, digest_items or [])
        
        # Composite score: Weight shift (volatility) and overlap (relevance)
        # We give a base score to overlap so even stable relevant markets can appear,
        # but shifts amplify them significantly.
        m["_score"] = (overlap * 2.0) + (shift * 1.5)
        scored.append(m)

    # Topic Deduplication: Keep only the highest scored market per topic cluster
    scored.sort(key=lambda x: x["_score"], reverse=True)
    final = []
    for m in scored:
        if any(_is_duplicate_topic(m, existing) for existing in final):
            continue
        final.append(m)
        if len(final) >= top_n:
            break

    return final
