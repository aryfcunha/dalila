"""ACAPS INFORM Severity Index ingestor.

ACAPS publishes the INFORM Severity Index — a 0–5 score capturing the
severity of humanitarian crises in ~140 countries, updated monthly.
The five named tiers sit at integer boundaries (Very Low / Low /
Medium / High / Very High at 1 / 2 / 3 / 4). Half-a-point movements
correspond to one tier-crossing and are surfaced in the brief.

Auth (verified 2026-06-26): token-exchange. POST username+password to
https://api.acaps.org/api/v1/token-auth/ → {"token": ...}; send it as
`Authorization: Token <token>`. Set ACAPS_USERNAME + ACAPS_PASSWORD; missing
creds → SourceSkipped.

API quirks (verified against the live endpoint):
- /api/v1/inform-severity-index/ 302-redirects to the current month
  (e.g. .../Jun2026/). Follow it.
- It REJECTS unknown query params with HTTP 404 ("Field X is not valid") —
  do NOT send `_format` or `limit`. Paginate via the DRF `next` URL.
- Scale is **0–10** (`INFORM Severity Index`, e.g. 9.5), NOT 0–5. There is a
  separate `INFORM Severity category (numeric)` 1–5 tier field.
- `iso3` and `country` are LISTS (e.g. ["AFG"], ["Afghanistan"]).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from dalila import db
from dalila.config import get_config, load_countries
from dalila.ingestors.forecast import (
    format_title, is_baseline_run, name_to_iso2_lookup, record_observation,
    resolve_iso2,
)
from dalila.models import RawItem

log = logging.getLogger(__name__)

ACAPS_TOKEN_URL = "https://api.acaps.org/api/v1/token-auth/"
INFORM_URL = "https://api.acaps.org/api/v1/inform-severity-index/"
INFORM_DELTA_THRESHOLD = 1.0     # a full point on the 0–10 INFORM Severity Index
SOURCE_ID = "inform"
_MAX_PAGES = 30                  # safety cap on DRF pagination (≈ thousands of rows)

# In-process token cache (mirrors acled.py). Token is exchanged once per process.
_token_cache: dict = {"token": None}


def _get_acaps_token() -> str | None:
    """Exchange ACAPS username/password for an API token (cached in-process).
    Returns None if creds are missing or the exchange fails."""
    if _token_cache["token"]:
        return _token_cache["token"]
    cfg = get_config()
    if not cfg.acaps_username or not cfg.acaps_password:
        return None
    try:
        resp = httpx.post(
            ACAPS_TOKEN_URL,
            data={"username": cfg.acaps_username, "password": cfg.acaps_password},
            timeout=30,
        )
        resp.raise_for_status()
        token = resp.json().get("token")
    except Exception as exc:
        log.warning("ACAPS token-auth failed: %s", exc)
        return None
    _token_cache["token"] = token
    return token


def _extract_rows(payload) -> list[dict]:
    """Pull the row list out of ACAPS's envelope. They've used multiple
    shapes over time — `results` (DRF default), `data`, or a bare list."""
    if isinstance(payload, dict):
        for key in ("results", "data", "items"):
            v = payload.get(key)
            if isinstance(v, list):
                return v
    if isinstance(payload, list):
        return payload
    return []


def _extract_score(row: dict) -> float | None:
    """The 0–10 INFORM Severity Index. The live field is 'INFORM Severity Index'
    (with spaces); the snake_case fallbacks cover any future shape change."""
    for key in ("INFORM Severity Index", "severity_index", "inform_severity_index",
                "current_severity", "severity_score", "score", "value"):
        raw = row.get(key)
        if raw is None:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def _first(v):
    """ACAPS returns iso3/country as lists; take the first element."""
    if isinstance(v, list):
        return v[0] if v else None
    return v


def _month_label(row: dict) -> str:
    raw = (
        row.get("Last updated") or row.get("_internal_filter_date")
        or row.get("date") or row.get("observation_date")
        or row.get("month") or row.get("updated_at") or ""
    )
    if not raw:
        return datetime.now(timezone.utc).strftime("%b %Y")
    try:
        return datetime.strptime(str(raw)[:7], "%Y-%m").strftime("%b %Y")
    except Exception:
        return str(raw)[:10]


def fetch(src: dict) -> list[RawItem]:
    cfg = get_config()
    if not cfg.acaps_username or not cfg.acaps_password:
        from dalila.ingestors.base import SourceSkipped
        raise SourceSkipped("ACAPS_USERNAME / ACAPS_PASSWORD not set")
    token = _get_acaps_token()
    if not token:
        # Creds present but token exchange failed → a real error, not a skip.
        raise RuntimeError("ACAPS token-auth failed (check ACAPS_USERNAME/PASSWORD)")

    headers = {"Authorization": f"Token {token}", "Accept": "application/json"}
    rows: list[dict] = []
    url = INFORM_URL   # 302-redirects to the current month; no query params (they 404)
    try:
        for _ in range(_MAX_PAGES):
            resp = httpx.get(url, headers=headers, timeout=60, follow_redirects=True)
            resp.raise_for_status()
            payload = resp.json()
            rows.extend(_extract_rows(payload))
            nxt = payload.get("next") if isinstance(payload, dict) else None
            if not nxt:
                break
            url = nxt
    except Exception as exc:
        log.warning("INFORM fetch failed: %s", exc)
        return []

    if not rows:
        log.info("INFORM: empty response")
        return []
    log.info("INFORM: fetched %d crisis rows", len(rows))

    name_lookup = name_to_iso2_lookup()
    items: list[RawItem] = []
    surfaced, stable = 0, 0

    with db.connect() as conn:
        seeding = is_baseline_run(conn, SOURCE_ID)
        if seeding:
            log.info("INFORM: first-ever run — recording baselines silently; "
                     "next monthly run will surface deltas ≥ %.1f only.",
                     INFORM_DELTA_THRESHOLD)
        for row in rows:
            # ACAPS iso3/country are LISTS — normalize before resolving.
            iso = resolve_iso2(
                {"iso3": _first(row.get("iso3")), "country": _first(row.get("country"))},
                name_lookup,
            )
            if not iso:
                continue
            score = _extract_score(row)
            if score is None:
                continue

            month_label = _month_label(row)
            observed_at = datetime.now(timezone.utc)
            notes = (str(_first(row.get("crisis_name")) or "").strip())[:1500] or None

            change = record_observation(
                conn, source_id=SOURCE_ID, country_iso2=iso,
                metric_key="severity",
                value=score, observed_at=observed_at,
                threshold_abs=INFORM_DELTA_THRESHOLD,
                notes=notes, seed_baseline=seeding,
            )
            if change is None:
                stable += 1
                continue
            surfaced += 1

            country_name = (load_countries()["countries"].get(iso) or {}).get("name") or iso
            title = format_title(
                change,
                source_label=f"INFORM {month_label}",
                country_name=country_name,
                metric_label="crisis severity",
                units="/10",
            )
            if change.value_prev is not None:
                body = (
                    f"ACAPS INFORM Severity Index for {country_name} moved from "
                    f"{change.value_prev:.1f} to {change.value_now:.1f} "
                    f"({'↑' if change.direction == 'up' else '↓'}{abs(change.delta or 0):.1f}) "
                    f"on the 0–10 scale. Observation date {month_label}."
                )
            else:
                body = (
                    f"ACAPS INFORM began tracking {country_name} at "
                    f"{change.value_now:.1f}/10 severity in {month_label}."
                )
            if notes:
                body += f" ACAPS note: {notes}"

            items.append(RawItem(
                source_id=src["id"],
                title=title[:300],
                url=f"https://www.acaps.org/en/countries/{iso.lower()}",
                body=body[:2000],
                published_at=observed_at,
                extra={"inform_severity": change.value_now, "inform_delta": change.delta},
            ))

    log.info("INFORM: %d countries surfaced (Δ ≥ %.1f), %d stable",
             surfaced, INFORM_DELTA_THRESHOLD, stable)
    return items
