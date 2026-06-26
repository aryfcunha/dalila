"""ACAPS INFORM Severity Index ingestor.

ACAPS publishes the INFORM Severity Index — a 0–5 score capturing the
severity of humanitarian crises in ~140 countries, updated monthly.
The five named tiers sit at integer boundaries (Very Low / Low /
Medium / High / Very High at 1 / 2 / 3 / 4). Half-a-point movements
correspond to one tier-crossing and are surfaced in the brief.

Auth: ACAPS_API_KEY env var (free public registration at api.acaps.org).
Without a key, the ingestor silently returns []. Same shape as `cast.py`.

CAUTION: ACAPS revised their API in 2025–2026. Endpoint paths and field
names below are inferred from their public tutorials. If the actual
response differs, the ingestor logs the keys it saw — adjust the field
extraction in `_extract_rows` and the score lookup accordingly.
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

# Public API root. The severity dataset lives at /api/v1/inform-severity-index/
# or /api/v1/inform-risk/ depending on which product ACAPS exposes.
# We try the severity endpoint first since that's the documented public one.
INFORM_URL = "https://api.acaps.org/api/v1/inform-severity-index/"
INFORM_DELTA_THRESHOLD = 0.5     # one tier-crossing on the 0–5 scale
SOURCE_ID = "inform"


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
    """Locate the severity score across the schema variants we've seen."""
    for key in ("severity_index", "inform_severity_index",
                "current_severity", "severity_score", "score", "value"):
        raw = row.get(key)
        if raw is None:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def _month_label(row: dict) -> str:
    raw = (
        row.get("date") or row.get("observation_date")
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
    api_key = getattr(cfg, "acaps_api_key", None)
    if not api_key:
        from dalila.ingestors.base import SourceSkipped
        raise SourceSkipped("ACAPS_API_KEY not set")

    try:
        resp = httpx.get(
            INFORM_URL,
            params={"_format": "json", "limit": 500},
            headers={"Authorization": f"Token {api_key}"},
            timeout=60,
            # ACAPS redirects /inform-severity-index/ to the latest monthly
            # snapshot (e.g. /inform-severity-index/May2026/). Follow it.
            follow_redirects=True,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        log.warning("INFORM fetch failed: %s", exc)
        return []

    rows = _extract_rows(payload)
    if not rows:
        keys = list(payload.keys()) if isinstance(payload, dict) else type(payload).__name__
        log.info("INFORM: empty response (envelope keys: %s)", keys)
        return []

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
            iso = resolve_iso2(row, name_lookup)
            if not iso:
                continue
            score = _extract_score(row)
            if score is None:
                continue

            month_label = _month_label(row)
            observed_at = datetime.now(timezone.utc)
            notes = (row.get("notes") or row.get("description") or "").strip()[:1500] or None

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
                units="/5",
            )
            if change.value_prev is not None:
                body = (
                    f"ACAPS INFORM Severity Index for {country_name} moved from "
                    f"{change.value_prev:.1f} to {change.value_now:.1f} "
                    f"({'↑' if change.direction == 'up' else '↓'}{abs(change.delta or 0):.1f}) "
                    f"on the 0–5 scale. Observation date {month_label}."
                )
            else:
                body = (
                    f"ACAPS INFORM began tracking {country_name} at "
                    f"{change.value_now:.1f}/5 severity in {month_label}."
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
