"""ACLED ingestor — armed conflict events.

ACLED migrated to OAuth 2.0 in 2026. The old `key=` + `email=` query-param
auth no longer works. Now:

1. Register a myACLED account at https://acleddata.com/user/register
2. Set ACLED_USERNAME (your registration email) + ACLED_PASSWORD in .env
3. We POST those to https://acleddata.com/oauth/token to mint a 24h
   `access_token`, cached in-process. The token is sent as a Bearer
   header on every /api/acled/read call.

Without creds, this ingestor silently returns []. We pull events from
the last 24 hours, filtered to a curated priority country set.
"""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Iterator

import httpx

from dalila.config import get_config
from dalila.models import RawItem

log = logging.getLogger(__name__)

# v2026: new base URL. Old api.acleddata.com host returns 404 / HTML now.
ACLED_BASE = "https://acleddata.com/api/acled/read"
ACLED_OAUTH_URL = "https://acleddata.com/oauth/token"

# Token cache — class-level so live ingest (every 30 min) and backfill share it.
_TOKEN_LOCK = threading.Lock()
_token_cache: dict = {"access_token": None, "expires_at": None}


def _get_access_token() -> str | None:
    """Return a fresh OAuth Bearer token (cached for 24h - safety margin).

    Returns None if creds are missing or the auth call fails. Callers should
    fall back to "skip ACLED this pass" — never raise.
    """
    cfg = get_config()
    if not cfg.acled_username or not cfg.acled_password:
        return None
    now = datetime.now(timezone.utc)
    with _TOKEN_LOCK:
        if _token_cache["access_token"] and _token_cache["expires_at"] and now < _token_cache["expires_at"]:
            return _token_cache["access_token"]
        try:
            resp = httpx.post(
                ACLED_OAUTH_URL,
                data={
                    "username": cfg.acled_username,
                    "password": cfg.acled_password,
                    "grant_type": "password",
                    "client_id": "acled",
                    "scope": "authenticated",
                },
                timeout=30,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            log.warning("ACLED oauth/token failed: %s", exc)
            return None
        tok = payload.get("access_token")
        if not tok:
            log.warning("ACLED oauth/token: no access_token in response: %s", str(payload)[:200])
            return None
        # ACLED returns expires_in (seconds). Default 24h; back off by 60s
        # so we never present an expired token mid-request.
        ttl = int(payload.get("expires_in") or 24 * 3600)
        _token_cache["access_token"] = tok
        _token_cache["expires_at"] = now + timedelta(seconds=max(ttl - 60, 60))
        log.info("ACLED oauth/token: minted new token, expires in %ds", ttl)
        return tok


def _auth_headers() -> dict:
    tok = _get_access_token()
    return {"Authorization": f"Bearer {tok}"} if tok else {}

# Country names ACLED uses — kept short for MVP, expand as needed
PRIORITY_COUNTRIES = [
    "Sudan", "Yemen", "Syria", "Ukraine",
    "Democratic Republic of Congo", "Ethiopia", "Somalia", "South Sudan",
    "Afghanistan", "Myanmar", "Lebanon", "Libya",
    "Mali", "Burkina Faso", "Niger",
    "Palestine",
]


def fetch(src: dict) -> list[RawItem]:
    cfg = get_config()
    if not cfg.acled_username or not cfg.acled_password:
        log.info("ACLED skipped: ACLED_USERNAME or ACLED_PASSWORD not set")
        return []
    headers = _auth_headers()
    if not headers:
        log.info("ACLED skipped: could not mint oauth token (check creds)")
        return []

    since = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    params = {
        "_format": "json",
        "event_date": f"{since}|{datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        "event_date_where": "BETWEEN",
        "country": "|".join(PRIORITY_COUNTRIES),
        "country_where": "IN",
        "limit": 200,
    }

    try:
        resp = httpx.get(ACLED_BASE, params=params, headers=headers, timeout=60)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        log.warning("ACLED fetch failed: %s", exc)
        return []

    if not payload.get("success"):
        log.warning("ACLED returned success=false: %s", payload.get("error"))
        return []

    items: list[RawItem] = []
    for ev in payload.get("data", []):
        country = ev.get("country") or "Unknown"
        event_type = ev.get("event_type") or "event"
        sub_event = ev.get("sub_event_type") or ""
        loc = ev.get("location") or ""
        notes = ev.get("notes") or ""
        fatalities = ev.get("fatalities") or 0
        date = ev.get("event_date")

        title = f"[{country}] {event_type}"
        if sub_event:
            title += f" — {sub_event}"
        if loc:
            title += f" in {loc}"
        if fatalities:
            title += f" ({fatalities} fatalities)"

        published_at: datetime | None = None
        if date:
            try:
                published_at = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except Exception:
                pass

        items.append(RawItem(
            source_id=src["id"],
            title=title[:300],
            url=ev.get("source_link") or None,
            body=notes[:1500] if notes else None,
            published_at=published_at,
            extra={"acled_event_id": ev.get("event_id_cnty")},
        ))
    return items


# ---------------------------------------------------------------------------
# Historical backfill — fetch events over an arbitrary date range.
# ---------------------------------------------------------------------------


def iter_items_range(
    source_id: str,
    since: date,
    until: date | None = None,
    *,
    page_size: int = 1000,
) -> Iterator[RawItem]:
    """Yield RawItems for every priority-country ACLED event in [since, until].

    Paginates with `page` until the API returns fewer than `page_size` rows.
    Same shape as `fetch()` but unbounded by the 24-hour window.
    """
    cfg = get_config()
    if not cfg.acled_username or not cfg.acled_password:
        log.warning("ACLED backfill skipped: ACLED_USERNAME or ACLED_PASSWORD not set")
        return
    headers = _auth_headers()
    if not headers:
        log.warning("ACLED backfill skipped: could not mint oauth token (check creds)")
        return
    until = until or date.today()
    page = 1
    fetched = 0
    while True:
        params = {
            "_format": "json",
            "event_date": f"{since.isoformat()}|{until.isoformat()}",
            "event_date_where": "BETWEEN",
            "country": "|".join(PRIORITY_COUNTRIES),
            "country_where": "IN",
            "limit": page_size,
            "page": page,
        }
        try:
            # Re-pull headers each request so an expired token gets refreshed
            # mid-walk without forcing the caller to restart the backfill.
            resp = httpx.get(ACLED_BASE, params=params, headers=_auth_headers(), timeout=90)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            log.warning("ACLED backfill page=%d failed: %s", page, exc)
            return
        if not payload.get("success"):
            log.warning("ACLED backfill returned success=false: %s", payload.get("error"))
            return
        rows = payload.get("data") or []
        if not rows:
            return
        for ev in rows:
            country = ev.get("country") or "Unknown"
            event_type = ev.get("event_type") or "event"
            sub_event = ev.get("sub_event_type") or ""
            loc = ev.get("location") or ""
            notes = ev.get("notes") or ""
            fatalities = ev.get("fatalities") or 0
            dt = ev.get("event_date")
            title = f"[{country}] {event_type}"
            if sub_event:
                title += f" — {sub_event}"
            if loc:
                title += f" in {loc}"
            if fatalities:
                title += f" ({fatalities} fatalities)"
            published_at: datetime | None = None
            if dt:
                try:
                    published_at = datetime.strptime(dt, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                except Exception:
                    pass
            yield RawItem(
                source_id=source_id,
                title=title[:300],
                url=ev.get("source_link") or None,
                body=notes[:1500] if notes else None,
                published_at=published_at,
                extra={"acled_event_id": ev.get("event_id_cnty")},
            )
            fetched += 1
        if len(rows) < page_size:
            return
        page += 1
        log.info("ACLED backfill: fetched %d rows so far (page %d)", fetched, page)
