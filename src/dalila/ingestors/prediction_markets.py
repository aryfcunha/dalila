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
def _search_manifold(query: str, limit: int = 5, min_vol: float | None = None) -> list[dict]:
    """Search Manifold for active binary markets. Returns list of market dicts."""
    try:
        url = (
            f"{MANIFOLD_BASE}/search-markets"
            f"?term={urllib.parse.quote(query)}"
            f"&limit={limit}&sort=liquidity&filter=open&contractType=BINARY"
        )
        data = _http_get(url, timeout=8)
        results = []
        if min_vol is None:
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


# ── Polymarket API ─────────────────────────────────────────────────────────────
POLYMARKET_GAMMA = "https://gamma-api.polymarket.com/events"

def _search_polymarket(query: str, limit: int = 5) -> list[dict]:
    """Search for Polymarket events via Gamma API."""
    try:
        url = f"{POLYMARKET_GAMMA}?active=true&q={urllib.parse.quote(query)}&limit={limit}"
        data = _http_get(url, timeout=10)
        results = []
        for event in (data if isinstance(data, list) else []):
            markets = event.get("markets", [])
            if not markets: continue
            
            # Polymarket events can have multiple markets; we take the first binary one
            m = markets[0]
            if m.get("marketType") != "normal": continue # only binary for now
            
            # outcomePrices is often a string of a JSON list like '["0.50", "0.50"]'
            prices = m.get("outcomePrices")
            if isinstance(prices, str):
                try:
                    prices = json.loads(prices)
                except Exception:
                    continue
            
            if not isinstance(prices, list) or len(prices) < 1:
                continue

            prob = float(prices[0]) # Assuming first is 'Yes'
            results.append({
                "source":      "polymarket",
                "market_id":   m["id"],
                "question":    m.get("question") or event.get("title"),
                "probability": prob,
                "volume":      float(m.get("volumeNum") or 0),
                "url":         f"https://polymarket.com/event/{event['slug']}",
            })
        return results
    except Exception as exc:
        log.debug("polymarket search failed for %r: %s", query, exc)
        return []

def _refresh_polymarket_prob(market_id: str) -> tuple[float | None, float | None]:
    """Refresh probability for a Polymarket market by ID."""
    try:
        url = f"https://gamma-api.polymarket.com/markets/{market_id}"
        m = _http_get(url, timeout=8)
        prices = m.get("outcomePrices")
        if isinstance(prices, str):
            prices = json.loads(prices)
        if isinstance(prices, list) and len(prices) >= 1:
            return float(prices[0]), float(m.get("volumeNum") or 0)
    except Exception:
        pass
    return None, None


# ── Metaforecast API (Metaculus Fallback) ──────────────────────────────────────
METAFORECAST_API = "https://metaforecast.org/api/graphql"

def _search_metaforecast(query: str, platforms: list[str] = ["metaculus"], limit: int = 5) -> list[dict]:
    """Search for forecasts across multiple platforms via Metaforecast GraphQL."""
    gql = """
    query Search($input: SearchInput!) {
      searchQuestions(input: $input) {
        id
        title
        url
        platform { id label }
        options { name probability }
      }
    }
    """
    variables = {
        "input": {
            "query": query,
            "forecastingPlatforms": platforms,
            "limit": limit
        }
    }
    try:
        body = json.dumps({"query": gql, "variables": variables}).encode("utf-8")
        req = urllib.request.Request(METAFORECAST_API, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            results = []
            for q in data.get("data", {}).get("searchQuestions", []):
                # We prioritize binary questions; take the probability of 'Yes' if available
                prob = None
                options = q.get("options") or []
                if len(options) == 2:
                    # Look for 'Yes' or similar
                    for opt in options:
                        if opt.get("name") in ["Yes", "True"]:
                            prob = opt.get("probability")
                    if prob is None: prob = options[0].get("probability") # Fallback
                elif len(options) == 1:
                    prob = options[0].get("probability")

                if prob is None: continue

                results.append({
                    "source":      q["platform"]["id"],
                    "market_id":   q["id"],
                    "question":    q["title"],
                    "probability": float(prob),
                    "volume":      1000.0, # Metaforecast doesn't always expose volume clearly
                    "url":         q["url"],
                })
            return results
    except Exception as exc:
        log.debug("metaforecast search failed for %r: %s", query, exc)
        return []


def _refresh_metaforecast_prob(market_id: str) -> tuple[float | None, float | None]:
    """Refresh a specific forecast's probability via ID."""
    gql = """
    query GetQuestion($id: ID!) {
      question(id: $id) {
        options { name probability }
      }
    }
    """
    try:
        body = json.dumps({"query": gql, "variables": {"id": market_id}}).encode("utf-8")
        req = urllib.request.Request(METAFORECAST_API, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
            q = data.get("data", {}).get("question")
            if not q: return None, None
            options = q.get("options") or []
            prob = None
            for opt in options:
                if opt.get("name") in ["Yes", "True"]:
                    prob = float(opt.get("probability"))
                    break
            if prob is None and options:
                prob = float(options[0].get("probability"))
            return prob, 1000.0
    except Exception:
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

    # Always include a selection of domain seeds to ensure regional consistency
    seeds = _s("discovery.domain_seeds", _DOMAIN_SEEDS)
    for seed in seeds:
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
                seen[mid]["_relevance_score"] = seen[mid].get("_relevance_score", 1) + 1

    # Search for priority seeds with a LOWER volume threshold (100) to catch niche signal
    for seed in seeds:
        # 1. Manifold
        for m in _search_manifold(seed, limit=3, min_vol=100.0):
            mid = m["market_id"]
            if mid not in seen:
                m["_relevance_score"] = 2.0 # Artificial boost for domain seeds
                seen[mid] = m
        
        # 2. Metaculus (via Metaforecast)
        for m in _search_metaforecast(seed, limit=2):
            mid = m["market_id"]
            if mid not in seen:
                m["_relevance_score"] = 2.0
                seen[mid] = m

        # 3. Polymarket
        for m in _search_polymarket(seed, limit=2):
            mid = m["market_id"]
            if mid not in seen:
                m["_relevance_score"] = 2.0
                seen[mid] = m

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
            elif src == "metaculus":
                prob, volume = _refresh_metaforecast_prob(mid)
            elif src == "polymarket":
                prob, volume = _refresh_polymarket_prob(mid)

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
            boost += 1.5

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
