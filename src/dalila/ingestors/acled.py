"""ACLED ingestor — armed conflict events.

Requires ACLED_API_KEY and ACLED_EMAIL. Without them, this ingestor silently
returns []. Sign up free (non-commercial license) at
https://acleddata.com/register-for-api-access/.

We pull events from the last 24 hours, filtered to a curated country set
matching our priority_countries in entities.yaml.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

from dalila.config import get_config
from dalila.models import RawItem

log = logging.getLogger(__name__)

ACLED_BASE = "https://api.acleddata.com/acled/read"

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
    if not cfg.acled_api_key or not cfg.acled_email:
        log.info("ACLED skipped: ACLED_API_KEY or ACLED_EMAIL not set")
        return []

    since = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    params = {
        "key": cfg.acled_api_key,
        "email": cfg.acled_email,
        "event_date": f"{since}|{datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        "event_date_where": "BETWEEN",
        "country": "|".join(PRIORITY_COUNTRIES),
        "country_where": "IN",
        "limit": 200,
    }

    try:
        resp = httpx.get(ACLED_BASE, params=params, timeout=60)
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
