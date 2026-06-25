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
# Kalshi's public read API. `api.elections.kalshi.com` is the current canonical
# host; `external-api.kalshi.com` is kept as a fallback (both answer today).
KALSHI_BASES  = [
    "https://api.elections.kalshi.com/trade-api/v2",
    "https://external-api.kalshi.com/trade-api/v2",
]
KALSHI_BASE   = KALSHI_BASES[0]  # back-compat alias
SETTINGS_PATH = Path(__file__).parents[3] / "prediction_markets.yaml"

# Curated Kalshi series tickers — Kalshi has no full-text search, and its open
# feed is dominated by elections + sports (e.g. a World-Cup-2026 flood) with the
# liquid markets buried, so blind paging is useless. We pull markets by series
# instead. These are the geopolitics / oil / macro series relevant to the UAE
# lens; override via prediction_markets.yaml → discovery.kalshi_series.
_KALSHI_SERIES = [
    "KXWTI",          # WTI crude oil price (daily mover, direct UAE/OPEC signal)
    "KXFED",          # Fed funds rate
    "KXFEDDECISION",  # Fed rate decision
    "KXCPI",          # US CPI / inflation
    "KXISRAELPM",     # Prime Minister of Israel
    "KXU3MAX",        # US unemployment ceiling
    "KXGDPYEAR",      # US GDP growth
]

_UA = {"User-Agent": "Dalila/0.1 (UAE intelligence digest; +https://github.com/aryfcunha/dalila)"}

# Permanent anchor seeds — always searched regardless of news cycle.
# These keep discovery anchored to the UAE / humanitarian / development lens.
# Edit prediction_markets.yaml → discovery.domain_seeds to override at runtime.
_DOMAIN_SEEDS = [
    # Israel / Gaza / Iran axis
    "Gaza Hamas",
    "Israel ceasefire",
    "Iran war",
    "Iran nuclear deal",
    "Strait of Hormuz",
    "Lebanon Hezbollah",
    # Gulf / Arabian Peninsula
    "Saudi Arabia Israel normalization",
    "Yemen Houthi",
    "OPEC production",
    "oil price 2026",
    # Broader MENA & conflict
    "Syria",
    "Sudan civil war",
    "Red Sea shipping",
    "Russia Ukraine ceasefire",
    # UAE direct signal
    "UAE",
]

# Token-level exclusion set — news seeds that duplicate anchor coverage are dropped.
_ANCHOR_TOKENS: frozenset[str] = frozenset(
    tok.lower() for seed in _DOMAIN_SEEDS for tok in seed.split()
)


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


# -- Relevance gate --------------------------------------------------------------
# Off-topic markets (sports, entertainment, celebrity health/death/personal life)
# have no place in a UAE humanitarian / development / geopolitics digest. They are
# dropped at discovery so they never reach the snapshots table, the website, or the
# daily brief. Extend at runtime via prediction_markets.yaml -> discovery.exclude_terms
# (those entries are ADDED to these built-in defaults).
_EXCLUDE_DEFAULTS = [
    # sports
    r"\bolympics?\b", r"\bworld cup\b", r"\bfifa\b", r"\buefa\b",
    r"\bpremier league\b", r"\bchampions league\b", r"\bnba\b", r"\bnfl\b",
    r"\bmlb\b", r"\bnhl\b", r"\bsuper bowl\b", r"\bcricket\b", r"\btwenty20\b",
    r"\bt20\b", r"\bipl\b", r"\bformula\s*1\b", r"\bf1\b", r"\bgrand prix\b",
    r"\bverstappen\b", r"\bwimbledon\b", r"\btennis\b", r"\bgolf\b",
    r"\bboxing\b", r"\bufc\b", r"\bgold medal\b", r"\bballon d'?or\b",
    r"\bplayoffs?\b", r"\bchampionship\b",
    # entertainment / awards
    r"\bgrammy", r"\boscars?\b", r"\bemmy", r"\beurovision\b",
    r"\bbox office\b", r"\bbillboard\b", r"\btaylor swift\b",
    # celebrity health / death / personal life
    r"\bbe alive\b", r"\bstill alive\b", r"\bremain alive\b", r"\bstay alive\b",
    r"\bbe dead\b", r"\bpass away\b", r"\bdie\b", r"\bdeath of\b",
    r"\bseriously ill\b", r"\bhave sex\b",
]

_EXCL_RE = None


def _excl_patterns():
    global _EXCL_RE
    if _EXCL_RE is not None:
        return _EXCL_RE
    extra = _s("discovery.exclude_terms", []) or []
    compiled = []
    for _p in list(_EXCLUDE_DEFAULTS) + [str(t) for t in extra]:
        try:
            compiled.append(re.compile(_p, re.IGNORECASE))
        except re.error:
            compiled.append(re.compile(re.escape(_p), re.IGNORECASE))
    _EXCL_RE = compiled
    return _EXCL_RE


def _is_relevant_question(question: str) -> bool:
    """False if the market question is off-topic (sports/entertainment/celebrity)."""
    q = question or ""
    return not any(rx.search(q) for rx in _excl_patterns())


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
            min_vol = float(_s("discovery.min_volume", 500))
        for m in (data if isinstance(data, list) else []):
            question = m.get("question", "")
            prob = m.get("probability")
            vol  = float(m.get("volume", 0))
            
            if prob is None or vol < min_vol:
                continue
            
            if not _is_relevant_question(question):
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
                "url":         m.get("url") or f"https://manifold.markets/{m.get('creatorUsername')}/{m.get('slug')}",
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
def _kalshi_get(path: str, timeout: int = 10) -> dict:
    """GET a Kalshi path, trying each base host until one answers."""
    last_exc: Exception | None = None
    for base in KALSHI_BASES:
        try:
            data = _http_get(base + path, timeout=timeout)
            return data if isinstance(data, dict) else {}
        except Exception as exc:  # noqa: BLE001 — try next host
            last_exc = exc
    if last_exc is not None:
        raise last_exc
    return {}


def _f(d: dict, *keys) -> float | None:
    """First parseable float among `keys` in dict `d` (Kalshi mixes str/num)."""
    for k in keys:
        v = d.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def _kalshi_prob(m: dict) -> float | None:
    """Implied YES probability from a Kalshi market object.

    Kalshi migrated its fields to `*_dollars` (already 0–1) / `*_fp`; the old
    integer-cent fields (`yes_bid`, `last_price`) are gone, which is why the
    previous reader always got None. We prefer the YES bid/ask midpoint, derive
    it from the NO side when only that is quoted, then fall back to last price,
    and finally to the legacy cent fields for safety.
    """
    yb = _f(m, "yes_bid_dollars")
    ya = _f(m, "yes_ask_dollars")
    if yb is None and ya is None:
        nb = _f(m, "no_bid_dollars")
        na = _f(m, "no_ask_dollars")
        if na is not None:
            yb = 1.0 - na          # yes_bid = 1 - no_ask
        if nb is not None:
            ya = 1.0 - nb          # yes_ask = 1 - no_bid
    if yb is not None and ya is not None:
        return max(0.0, min(1.0, (yb + ya) / 2.0))
    lp = _f(m, "last_price_dollars")
    if lp is not None:
        return max(0.0, min(1.0, lp))
    # Legacy cent fields (pre-migration), just in case a host still serves them.
    yb, ya, lp = m.get("yes_bid"), m.get("yes_ask"), m.get("last_price")
    if yb is not None and ya is not None:
        return (yb + ya) / 200.0
    if lp is not None:
        return lp / 100.0
    return None


def _kalshi_open_interest(m: dict) -> float:
    """Open interest (contracts) — our liquidity proxy for Kalshi.

    `volume` is unreliable / absent on the series-filtered endpoint, but open
    interest is consistently populated and is a sound proxy for how live a
    market is."""
    return _f(m, "open_interest_fp", "open_interest") or 0.0


def _refresh_kalshi_prob(ticker: str) -> tuple[float | None, float | None]:
    """Fetch latest (probability, open_interest) for a known Kalshi ticker."""
    for path in [
        f"/markets/{ticker.upper()}",
        f"/events/{ticker.upper()}?with_nested_markets=true",
    ]:
        try:
            data = _kalshi_get(path, timeout=8)
        except Exception:
            continue
        market = data.get("market") or {}
        if not market:
            markets = (data.get("event") or {}).get("markets") or data.get("markets") or []
            market = markets[0] if markets else {}
        if not market:
            continue
        prob = _kalshi_prob(market)
        if prob is not None:
            return prob, _kalshi_open_interest(market)
    return None, None


def _search_kalshi(
    series_tickers: list[str],
    *,
    min_open_interest: float = 0.0,
    per_series_keep: int = 3,
    max_months_out: int = 18,
    limit_per_series: int = 100,
) -> list[dict]:
    """Discover liquid-ish Kalshi markets from a curated set of series tickers.

    For each series we pull its open markets, keep the most-active ones by open
    interest (Kalshi's reliable liquidity signal), and drop long-dated contracts
    that won't move day-to-day. Errors on one series never sink the others.
    """
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=30 * max_months_out)
    out: list[dict] = []

    for st in series_tickers:
        try:
            data = _kalshi_get(
                f"/markets?series_ticker={urllib.parse.quote(st)}"
                f"&status=open&limit={limit_per_series}",
                timeout=8,
            )
        except Exception as exc:
            log.debug("kalshi series %s failed: %s", st, exc)
            continue

        candidates: list[tuple[float, dict]] = []
        for m in (data.get("markets") or []):
            prob = _kalshi_prob(m)
            if prob is None:
                continue
            oi = _kalshi_open_interest(m)
            if oi < min_open_interest:
                continue
            # Drop long-dated contracts (won't be daily movers).
            close = m.get("close_time") or m.get("expiration_time")
            if close:
                try:
                    cdt = datetime.fromisoformat(close.replace("Z", "+00:00"))
                    if cdt > cutoff:
                        continue
                except Exception:
                    pass
            ticker = m.get("ticker")
            if not ticker:
                continue
            title = (m.get("title") or "").strip()
            sub = (m.get("yes_sub_title") or m.get("subtitle") or "").strip()
            question = title
            if sub and sub.lower() not in title.lower():
                question = f"{title} — {sub}" if title else sub
            # Same long-term-year guard as Manifold.
            if re.findall(r"\b20[3-9][0-9]\b", question):
                continue
            event_ticker = m.get("event_ticker") or ticker
            candidates.append((oi, {
                "source":      "kalshi",
                "market_id":   ticker,
                "question":    question or ticker,
                "probability": prob,
                "volume":      oi,
                "url":         f"https://kalshi.com/markets/{event_ticker}",
            }))

        # Keep the most active markets per series so one series can't flood the
        # pool with dozens of dead strike-ladder rungs.
        candidates.sort(key=lambda x: x[0], reverse=True)
        out.extend(m for _, m in candidates[:per_series_keep])

    return out


# ── Polymarket API ─────────────────────────────────────────────────────────────
POLYMARKET_GAMMA = "https://gamma-api.polymarket.com/events"

def _search_polymarket(query: str, limit: int = 5, min_vol: float = 0.0) -> list[dict]:
    """Search for liquid binary Polymarket markets via the Gamma API.

    Polymarket is the best-fit liquid, daily-moving source for the geopolitics /
    oil / MENA lens, but the Gamma host is geo-blocked from some networks (it
    refuses the TLS connection), so callers must treat an empty list as "either
    no results or unreachable" and degrade gracefully.
    """
    try:
        url = (
            f"{POLYMARKET_GAMMA}?active=true&closed=false"
            f"&q={urllib.parse.quote(query)}&limit={limit}"
        )
        data = _http_get(url, timeout=8)
        results = []
        for event in (data if isinstance(data, list) else []):
            markets = event.get("markets") or []
            if not markets:
                continue
            # An event can bundle several markets; take the most liquid binary one.
            best = None
            best_vol = -1.0
            for m in markets:
                # A binary market has exactly two outcomes ("Yes"/"No"). Gamma
                # doesn't reliably set `marketType`, so detect by outcome count
                # rather than gating on it (the old `== "normal"` check dropped
                # everything).
                prices = m.get("outcomePrices")
                if isinstance(prices, str):
                    try:
                        prices = json.loads(prices)
                    except Exception:
                        continue
                if not isinstance(prices, list) or len(prices) != 2:
                    continue
                vol = float(m.get("volumeNum") or m.get("volume") or 0)
                if vol > best_vol:
                    best, best_vol = (m, prices), vol
            if best is None:
                continue
            m, prices = best
            if best_vol < min_vol:
                continue
            question = m.get("question") or event.get("title") or ""
            if re.findall(r"\b20[3-9][0-9]\b", question):
                continue
            try:
                prob = float(prices[0])  # first outcome = 'Yes'
            except (TypeError, ValueError):
                continue
            slug = event.get("slug")
            results.append({
                "source":      "polymarket",
                "market_id":   str(m.get("id") or m.get("conditionId") or slug),
                "question":    question,
                "probability": prob,
                "volume":      best_vol,
                "url":         f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com",
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


def _metaforecast_ping() -> bool:
    """Network-reachability probe for Metaforecast. Returns True if the host
    answers at all (even a GraphQL error body counts — that just means the
    search resolver is degraded, not that the network is dead); raises on a
    transport-level failure so the caller can skip the source this cycle."""
    body = json.dumps({"query": "{ __typename }"}).encode("utf-8")
    req = urllib.request.Request(
        METAFORECAST_API, data=body,
        headers={"Content-Type": "application/json", **_UA},
    )
    with urllib.request.urlopen(req, timeout=6) as resp:
        return resp.status < 500 or True  # any HTTP response = reachable

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


# ── News-derived seed extraction ──────────────────────────────────────────────
def _extract_news_seeds(
    conn: sqlite3.Connection, *, lookback_days: int = 14, limit: int = 8
) -> list[str]:
    """Derive market search seeds from recent UAE-relevant classified news.

    Pulls named entities from classified items with uae_relevance >= 0.5 over
    the last `lookback_days`. Only proper nouns (first char uppercase, ≥ 4 chars)
    that don't overlap with the permanent anchor seeds are returned.

    This lets the market watchlist evolve with the news cycle — new crises,
    summits, and aid commitments surface automatically — while the anchor seeds
    guarantee baseline UAE / humanitarian / development coverage.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    rows = conn.execute(
        """SELECT entities_json FROM items
           WHERE ingested_at >= ?
             AND classified_at IS NOT NULL
             AND uae_relevance >= 0.5
           ORDER BY ingested_at DESC
           LIMIT 1000""",
        (cutoff,),
    ).fetchall()

    freq: dict[str, int] = {}
    for row in rows:
        try:
            for ent in (json.loads(row[0] or "[]") or []):
                name = (ent.get("name") or "").strip()
                if (
                    len(name) >= 4
                    and name[0].isupper()
                    # drop anything whose tokens fully overlap with anchors
                    and not all(tok.lower() in _ANCHOR_TOKENS for tok in name.split())
                ):
                    freq[name] = freq.get(name, 0) + 1
        except Exception:
            pass

    ranked = sorted(freq.items(), key=lambda x: x[1], reverse=True)
    return [term for term, _ in ranked[:limit]]


# ── Market discovery ──────────────────────────────────────────────────────────
def _reachable(name: str, probe) -> bool:
    """Cheap one-shot reachability check so a blocked/down source is skipped for
    the whole cycle instead of failing once per seed (Polymarket is geo-blocked
    from some networks; Metaforecast's search has been 500-ing upstream)."""
    try:
        probe()
        return True
    except Exception as exc:
        log.info("prediction markets: %s unreachable this cycle (%s) — skipping",
                 name, exc.__class__.__name__)
        return False


def discover_markets(conn: sqlite3.Connection) -> list[dict]:
    """Discover markets across all reachable sources, anchored to the UAE lens.

    Sources:
      * Manifold  — keyword search per seed (on-topic but low-liquidity).
      * Polymarket — keyword search per seed (liquid, daily-moving; geo-blocked
        on some networks, so degrades to nothing when unreachable).
      * Kalshi    — curated geopolitics/oil/macro series (no full-text search).
      * Metaforecast/Metaculus — keyword search per seed (forecasts; upstream
        search is currently flaky, so also degrades gracefully).

    Permanent anchor seeds keep results grounded in the UAE / humanitarian /
    development lens. Dynamic seeds are extracted from recent high-UAE-relevance
    classified items so the watchlist evolves with the news cycle.

    Each source is independent: one being blocked, down, or empty never starves
    the others, and results are merged round-robin so the pool keeps a healthy
    cross-source mix rather than collapsing to whichever source is most liquid.
    """
    anchor_seeds: list[str] = _s("discovery.domain_seeds", _DOMAIN_SEEDS)
    news_seeds = _extract_news_seeds(conn)

    anchor_lower = {s.lower() for s in anchor_seeds}
    extra_seeds = [s for s in news_seeds if s.lower() not in anchor_lower]
    seeds = list(anchor_seeds) + extra_seeds

    max_markets = int(_s("discovery.max_markets", 50))
    min_vol     = float(_s("discovery.min_volume", 500))
    src_cfg     = _s("discovery.sources", {}) or {}

    def _on(name: str) -> bool:
        return bool(src_cfg.get(name, True))

    # per-source accumulator: source -> {market_id -> dict}
    pools: dict[str, dict[str, dict]] = {
        s: {} for s in ("manifold", "polymarket", "kalshi", "metaculus")
    }

    def _add(m: dict) -> None:
        src = m.get("source")
        bucket = pools.setdefault(src, {})
        mid = m["market_id"]
        if mid not in bucket:
            bucket[mid] = m
        else:
            bucket[mid]["_relevance_score"] = bucket[mid].get("_relevance_score", 1) + 1

    # Pre-flight the network-fragile sources once so we don't pay a failure per
    # seed when they're blocked/down.
    poly_on = _on("polymarket") and _reachable(
        "polymarket",
        lambda: _http_get(f"{POLYMARKET_GAMMA}?limit=1&active=true", timeout=6),
    )
    meta_on = _on("metaforecast") and _reachable(
        "metaforecast", _metaforecast_ping,
    )

    # Per-seed keyword sources.
    for seed in seeds:
        if _on("manifold"):
            for m in _search_manifold(seed, limit=5, min_vol=min_vol):
                _add(m)
        if poly_on:
            for m in _search_polymarket(seed, limit=5, min_vol=min_vol):
                _add(m)
        if meta_on:
            for m in _search_metaforecast(seed, limit=5):
                _add(m)

    # Kalshi: one curated-series sweep, not per-seed.
    if _on("kalshi"):
        series = _s("discovery.kalshi_series", _KALSHI_SERIES)
        for m in _search_kalshi(
            series,
            min_open_interest=float(_s("discovery.kalshi_min_open_interest", 0)),
            per_series_keep=int(_s("discovery.kalshi_per_series", 3)),
        ):
            _add(m)

    # Rank within each source (multi-seed matches first, then liquidity).
    ranked_by_source: dict[str, list[dict]] = {
        src: sorted(bucket.values(),
                    key=lambda x: (x.get("_relevance_score", 1), x.get("volume", 0)),
                    reverse=True)
        for src, bucket in pools.items() if bucket
    }

    # Round-robin merge for a cross-source mix; leftovers fill any remaining slots.
    merged: list[dict] = []
    order = [s for s in ("manifold", "polymarket", "kalshi", "metaculus")
             if s in ranked_by_source]
    i = 0
    while len(merged) < max_markets and any(ranked_by_source.values()):
        src = order[i % len(order)] if order else None
        i += 1
        if src and ranked_by_source.get(src):
            merged.append(ranked_by_source[src].pop(0))
        if i > max_markets * len(order) + 10:  # safety bound
            break

    log.info(
        "prediction markets: discovered %d markets across %s "
        "(%d seeds: %d anchors + %d news)",
        len(merged),
        {s: len(b) for s, b in ranked_by_source.items()} or
        {s: len(p) for s, p in pools.items() if p},
        len(seeds), len(anchor_seeds), len(extra_seeds),
    )
    return merged[:max_markets]


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
               (market_id, source, question, probability, volume, url, topic_tags, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(market_id, source) DO UPDATE SET
               probability = excluded.probability,
               volume      = COALESCE(excluded.volume, prediction_market_snapshots.volume),
               url         = COALESCE(excluded.url, prediction_market_snapshots.url),
               question    = excluded.question,
               recorded_at = excluded.recorded_at""",
        (m["market_id"], m["source"], m.get("question", m["market_id"]),
         prob, volume, m.get("url"), None, now),
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
        if not _is_relevant_question(m.get("question", "")):
            continue
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

    # Evict snapshots not refreshed in the last 48 hours — they are no longer
    # being discovered by the current seed set and should age out quietly.
    cutoff_48h = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    conn.execute(
        "DELETE FROM prediction_market_snapshots WHERE recorded_at < ?",
        (cutoff_48h,),
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


def tracked_market_count(conn: sqlite3.Connection) -> int:
    """How many markets are currently in the snapshot pool (the background set
    we keep polling, regardless of whether they've moved)."""
    try:
        return int(conn.execute(
            "SELECT COUNT(*) FROM prediction_market_snapshots"
        ).fetchone()[0])
    except Exception:
        return 0


def _max_abs_move(m: dict) -> float:
    """Largest absolute move across the available delta windows (30m/24h/1w).
    Windows with no baseline (None) are ignored. Returns 0.0 if none exist."""
    moves = [abs(m[k]) for k in ("delta_30m", "delta_24h", "delta_7d")
             if m.get(k) is not None]
    return max(moves) if moves else 0.0


def get_market_signals(conn: sqlite3.Connection,
                       digest_items: list[dict] | None = None,
                       top_n: int | None = None,
                       *,
                       live_only: bool = False,
                       live_threshold: float | None = None) -> list[dict]:
    """Return top N market signals scored for today's digest content.

    `live_only` (used by the public website) keeps only markets that actually
    moved — at least `live_threshold` absolute probability shift in any of the
    30m / 24h / 1w windows — and ranks them by how much they moved. This stops
    a pool of static, low-liquidity markets from rendering as a wall of
    "+0.0%" cards that makes the site look broken. The background pool is left
    untouched; we just don't surface the quiet ones.
    """
    if top_n is None:
        top_n = int(_s("digest.top_n", 9))
    if live_threshold is None:
        live_threshold = float(_s("display.live_threshold", 0.005))

    now = datetime.now(timezone.utc)
    # Each window's baseline must be BRACKETED near its nominal age, not merely
    # "the most recent row at least N old". Without a lower bound, when polling
    # has been down (e.g. the 2026-05-14→06-25 history gap), the only row that
    # exists is an ancient pre-gap snapshot — and that single row satisfies the
    # `recorded_at <= cutoff` test for ALL THREE windows, so p_30m == p_24h ==
    # p_7d and the deltas collapse to three identical numbers mislabelled as
    # 30m/24h/1w. The lower bound rejects a baseline that's far older than the
    # window; if nothing qualifies the delta is reported as None ("--") rather
    # than computed against stale data. Slack on each side absorbs missed polls.
    hi_30m = (now - timedelta(minutes=25)).isoformat()
    lo_30m = (now - timedelta(hours=2)).isoformat()
    hi_24h = (now - timedelta(hours=23)).isoformat()
    lo_24h = (now - timedelta(hours=36)).isoformat()
    hi_7d  = (now - timedelta(hours=167)).isoformat()
    lo_7d  = (now - timedelta(days=10)).isoformat()

    # Use a two-step join (aggregate → history) so we always retrieve the
    # probability from the exact row with MAX(recorded_at) inside the window's
    # [lo, hi] bracket, not an arbitrary row as a bare-column aggregation would.
    rows = conn.execute(
        """SELECT s.market_id, s.source, s.question, s.probability, s.volume, s.url,
                  h24.probability as p_24h,
                  h7d.probability as p_7d,
                  h30m.probability as p_30m
           FROM prediction_market_snapshots s
           LEFT JOIN (
               SELECT h.market_id, h.source, h.probability
               FROM prediction_market_history h
               INNER JOIN (
                   SELECT market_id, source, MAX(recorded_at) as max_at
                   FROM prediction_market_history WHERE recorded_at <= ? AND recorded_at >= ?
                   GROUP BY market_id, source
               ) m ON h.market_id = m.market_id AND h.source = m.source AND h.recorded_at = m.max_at
           ) h24 ON h24.market_id = s.market_id AND h24.source = s.source
           LEFT JOIN (
               SELECT h.market_id, h.source, h.probability
               FROM prediction_market_history h
               INNER JOIN (
                   SELECT market_id, source, MAX(recorded_at) as max_at
                   FROM prediction_market_history WHERE recorded_at <= ? AND recorded_at >= ?
                   GROUP BY market_id, source
               ) m ON h.market_id = m.market_id AND h.source = m.source AND h.recorded_at = m.max_at
           ) h7d ON h7d.market_id = s.market_id AND h7d.source = s.source
           LEFT JOIN (
               SELECT h.market_id, h.source, h.probability
               FROM prediction_market_history h
               INNER JOIN (
                   SELECT market_id, source, MAX(recorded_at) as max_at
                   FROM prediction_market_history WHERE recorded_at <= ? AND recorded_at >= ?
                   GROUP BY market_id, source
               ) m ON h.market_id = m.market_id AND h.source = m.source AND h.recorded_at = m.max_at
           ) h30m ON h30m.market_id = s.market_id AND h30m.source = s.source""",
        (hi_24h, lo_24h, hi_7d, lo_7d, hi_30m, lo_30m),
    ).fetchall()

    scored = []
    for row in rows:
        p_new = row[3]
        p_24h = row[6]
        p_7d  = row[7]
        
        m = {
            "market_id":   row[0], 
            "source":      row[1],
            "question":    row[2], 
            "probability": p_new,
            "volume":      row[4],
            "url":         row[5],
            "delta_30m":   (p_new - row[8]) if row[8] is not None else None,
            "delta_24h":   (p_new - p_24h) if p_24h is not None else None,
            "delta_7d":    (p_new - p_7d)  if p_7d is not None else None,
        }
        
        # Scoring: Log-odds shift captures 1% -> 10% movements better than absolute p.p.
        if p_24h is not None:
            shift = abs(_logit(p_new) - _logit(p_24h))
        else:
            shift = 0.0
            
        overlap = _topic_overlap(m, digest_items or [])
        m["_move"] = _max_abs_move(m)
        m["_score"] = (overlap * 2.0) + (shift * 1.5)
        scored.append(m)

    # Live filter: drop markets that haven't moved meaningfully in any window.
    if live_only:
        scored = [m for m in scored if m["_move"] >= live_threshold]
        # Rank the survivors by how much they moved (liveliest first); the
        # public page is not tied to a specific digest, so movement — not
        # topic overlap — is the right ordering there.
        scored.sort(key=lambda x: (x["_move"], x.get("volume") or 0), reverse=True)
    else:
        scored.sort(key=lambda x: x["_score"], reverse=True)

    # Topic Deduplication: Keep only the highest-ranked market per topic cluster
    final = []
    for m in scored:
        if any(_is_duplicate_topic(m, existing) for existing in final):
            continue
        final.append(m)
        if len(final) >= top_n:
            break

    return final
