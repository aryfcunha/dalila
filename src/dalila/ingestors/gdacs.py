"""GDACS (Global Disaster Alert and Coordination System) ingestor.

GDACS publishes real-time alerts for natural disasters with a categorical
severity tier (Green / Orange / Red, plus Extreme for the most severe
events). Tier transitions are the meaningful events:

  - Green → Orange / Orange → Red / Red → Extreme  ⇒ escalation
  - Red → Orange / Orange → Green                  ⇒ de-escalation
  - new alert appearing in the feed                ⇒ surfaced as `new`
  - alert dropping out of the feed                 ⇒ surfaced as `cleared`

Because GDACS is a feed of *currently active* alerts, the absence of a
country-event_type pair from the feed means there's no current alert.
Each ingest pass therefore needs to detect both new appearances and
silent disappearances. We achieve this with a two-stage process:

  1. Read the live feed, record_observation() each (country, event_type)
     with its current alert level → surfaces escalations / new alerts.
  2. After the pass, walk forecast_snapshots for this source where the
     stored text was non-empty but no observation came in this run →
     surface those as 'cleared' alerts.

Event types in GDACS taxonomy: EQ (earthquake), TC (tropical cyclone),
FL (flood), DR (drought), VO (volcano), WF (wildfire). We use the full
event-type label in the title so the brief reads naturally.

Public RSS endpoint, no auth required:
  https://www.gdacs.org/xml/rss.xml
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import feedparser

from dalila import db
from dalila.config import load_countries
from dalila.ingestors.forecast import (
    ForecastChange, is_baseline_run, name_to_iso2_lookup,
    record_observation, resolve_iso2,
)
from dalila.models import RawItem

log = logging.getLogger(__name__)

GDACS_RSS = "https://www.gdacs.org/xml/rss.xml"
SOURCE_ID = "gdacs"

# GDACS RSS embeds a custom namespace ("gdacs:...") with structured fields.
# feedparser exposes them under entry.get("gdacs_alertlevel") etc.
_EVENT_TYPE_LABELS = {
    "EQ": "earthquake",
    "TC": "tropical cyclone",
    "FL": "flood",
    "DR": "drought",
    "VO": "volcanic activity",
    "WF": "wildfire",
}

# Tier ordering for direction inference. Higher index = more severe.
_TIER_ORDER = {"green": 0, "orange": 1, "red": 2, "extreme": 3}


def _direction_from_tier(prev: str | None, now: str | None) -> str:
    p = (prev or "").lower(); n = (now or "").lower()
    pi = _TIER_ORDER.get(p, -1); ni = _TIER_ORDER.get(n, -1)
    if not p and n:        return "new"
    if p and not n:        return "cleared"
    if pi < 0 or ni < 0:   return "change"
    if ni > pi:            return "up"      # escalation → 🔴
    if ni < pi:            return "down"    # de-escalation → 🟢
    return "change"


def _extract_country_iso(entry, name_lookup: dict[str, str]) -> str | None:
    iso3 = (entry.get("gdacs_iso3") or "").strip().upper()
    if iso3:
        # Reuse the shared alpha-3 → alpha-2 map via resolve_iso2.
        return resolve_iso2({"iso3": iso3}, name_lookup)
    name = (entry.get("gdacs_country") or "").strip().lower()
    if name:
        # Take only the first country if there are multiple (comma-separated).
        first = name.split(",")[0].strip()
        return name_lookup.get(first)
    return None


def fetch(src: dict) -> list[RawItem]:
    try:
        parsed = feedparser.parse(GDACS_RSS)
    except Exception as exc:
        log.warning("GDACS feed parse failed: %s", exc)
        return []
    if parsed.bozo and not parsed.entries:
        log.warning("GDACS feed empty: %s", getattr(parsed, "bozo_exception", "?"))
        return []

    name_lookup = name_to_iso2_lookup()
    items: list[RawItem] = []
    seen_keys: set[str] = set()    # (iso, event_type) we saw this pass
    observed_at = datetime.now(timezone.utc)

    with db.connect() as conn:
        seeding = is_baseline_run(conn, SOURCE_ID)
        if seeding:
            log.info("GDACS: first-ever run — recording current alerts as "
                     "baseline; tier changes and new alerts surface next pass.")

        for entry in parsed.entries:
            iso = _extract_country_iso(entry, name_lookup)
            if not iso:
                continue
            event_type = (entry.get("gdacs_eventtype") or "").strip().upper()
            if not event_type:
                continue
            tier = (entry.get("gdacs_alertlevel") or "").strip().lower()
            if tier not in _TIER_ORDER:
                continue

            metric_key = f"alert_{event_type}"
            seen_keys.add(f"{iso}|{metric_key}")

            event_name = (entry.get("gdacs_eventname")
                          or entry.get("title") or "").strip()
            notes = (entry.get("summary") or "").strip()[:1500] or None

            change = record_observation(
                conn, source_id=SOURCE_ID, country_iso2=iso,
                metric_key=metric_key,
                text=tier, observed_at=observed_at,
                threshold_text_change=True,
                notes=notes, seed_baseline=seeding,
            )
            if change is None:
                continue
            # Set the directional emoji properly for GDACS — for tier rises
            # the helper would have returned `change` direction, but it
            # doesn't know our tier order. Override:
            change.direction = _direction_from_tier(change.text_prev, change.text_now)

            country_name = (load_countries()["countries"].get(iso) or {}).get("name") or iso
            event_label = _EVENT_TYPE_LABELS.get(event_type, event_type.lower())
            if change.direction == "new":
                title = (
                    f"{change.emoji} [GDACS] New {tier} {event_label} alert: "
                    f"{event_name or country_name}"
                )
            elif change.direction == "cleared":
                title = (
                    f"{change.emoji} [GDACS] {event_label.capitalize()} alert "
                    f"cleared in {country_name} (was {change.text_prev})"
                )
            else:
                title = (
                    f"{change.emoji} [GDACS] {country_name} {event_label} alert "
                    f"{tier} (was {change.text_prev})"
                )
            body = entry.get("summary") or (
                f"{event_label.capitalize()} event {event_name or '(unnamed)'} "
                f"in {country_name}; current alert level {tier}."
            )
            items.append(RawItem(
                source_id=src["id"],
                title=title[:300],
                url=entry.get("link") or f"https://www.gdacs.org/?country={iso}",
                body=body[:2000],
                published_at=observed_at,
                extra={"gdacs_event_type": event_type, "gdacs_alert_level": tier},
            ))

        # Pass 2: detect alerts that disappeared from the feed → "cleared".
        if not seeding:
            stale = conn.execute(
                """
                SELECT country_iso2, metric_key, value_text
                FROM forecast_snapshots
                WHERE source_id = ? AND value_text IS NOT NULL AND value_text != ''
                """,
                (SOURCE_ID,),
            ).fetchall()
            for r in stale:
                key = f"{r['country_iso2']}|{r['metric_key']}"
                if key in seen_keys:
                    continue
                # This alert is no longer in the feed; clear it.
                cleared = record_observation(
                    conn, source_id=SOURCE_ID,
                    country_iso2=r["country_iso2"],
                    metric_key=r["metric_key"],
                    text=None, observed_at=observed_at,
                    threshold_text_change=True,
                )
                if cleared is None:
                    continue
                cleared.direction = "cleared"
                iso = r["country_iso2"]
                event_type = r["metric_key"].replace("alert_", "")
                country_name = (load_countries()["countries"].get(iso) or {}).get("name") or iso
                event_label = _EVENT_TYPE_LABELS.get(event_type, event_type.lower())
                items.append(RawItem(
                    source_id=src["id"],
                    title=(f"{cleared.emoji} [GDACS] {event_label.capitalize()} alert "
                           f"cleared in {country_name} (was {r['value_text']})")[:300],
                    url=f"https://www.gdacs.org/?country={iso}",
                    body=(f"GDACS {event_label} alert for {country_name} is no longer "
                          f"in the active-events feed; previous level: {r['value_text']}."),
                    published_at=observed_at,
                    extra={"gdacs_event_type": event_type, "gdacs_alert_level": "cleared"},
                ))

    log.info("GDACS: %d items surfaced", len(items))
    return items
