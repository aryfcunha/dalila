"""WFP HungerMap LIVE ingestor.

HungerMap LIVE publishes daily-updated estimates of the number of people
with insufficient food consumption per country. It's the most timely
food-insecurity signal at country granularity in the public domain.

The change threshold from METHODOLOGY.md:
  (≥10% relative AND ≥100k absolute) OR ≥500k absolute

  - Relative gate flags real movement, the 100k absolute floor blocks
    small-population countries from triggering on percentage noise
    (10% of 50,000 is 5,000 — not news).
  - The ≥500k absolute branch always fires regardless of percentage,
    so a large-population country whose count shifted by 600k passes
    even if that's only a 3% change.

The shared `record_observation` helper supports this compound rule via
`threshold_min_abs_for_rel` paired with `threshold_rel` (AND-gated),
plus `threshold_abs` (OR-branch).

Daily polling is fine — the snapshot dedup means stable countries are
silent. The brief only ever sees movements.

Endpoint: https://api.hungermapdata.org/v1/foodsecurity/country
Auth: public; no key required (as of 2026-05).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from dalila import db
from dalila.config import load_countries
from dalila.ingestors.forecast import (
    format_title, is_baseline_run, name_to_iso2_lookup, record_observation,
    resolve_iso2,
)
from dalila.models import RawItem

log = logging.getLogger(__name__)

HUNGERMAP_URL = "https://api.hungermapdata.org/v1/foodsecurity/country"
HUNGERMAP_REL_THRESHOLD = 0.10           # 10% week-over-week
HUNGERMAP_MIN_ABS_FOR_REL = 100_000      # noise floor under the relative gate
HUNGERMAP_ABS_THRESHOLD = 500_000        # "always notable" absolute shift
SOURCE_ID = "hungermap"


def _extract_rows(payload) -> list[dict]:
    """Unwrap HungerMap's response. They serve responses behind an AWS
    Lambda proxy that wraps everything in {"statusCode": …, "body": "<json>"},
    where `body` is itself a JSON string. Unwrap that first, then look
    for the country list in the inner payload."""
    import json as _json
    if isinstance(payload, dict) and "body" in payload and "statusCode" in payload:
        body = payload["body"]
        if isinstance(body, str):
            try:
                payload = _json.loads(body)
            except Exception as exc:
                log.debug("HungerMap: failed to parse body string: %s", exc)
        elif isinstance(body, (dict, list)):
            payload = body
    if isinstance(payload, dict):
        for key in ("countries", "data", "results", "body"):
            v = payload.get(key)
            if isinstance(v, list):
                return v
        if "country" in payload and isinstance(payload["country"], list):
            return payload["country"]
    if isinstance(payload, list):
        return payload
    return []


def _extract_count(row: dict) -> float | None:
    """People with insufficient food consumption (the headline metric).
    HungerMap nests it under various keys depending on payload shape."""
    fcs = row.get("fcs") or row.get("food_consumption") or {}
    if isinstance(fcs, dict):
        for key in ("people", "people_with_insufficient_food_consumption",
                    "insufficient_food_consumption", "count"):
            raw = fcs.get(key)
            if raw is not None:
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    continue
    for key in ("people_with_insufficient_food_consumption",
                "insufficient_food_consumption",
                "people_affected", "value"):
        raw = row.get(key)
        if raw is None:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def _fmt_count(n: float) -> str:
    """Render large counts as human-readable units (1.2M / 540k / 23k)."""
    if n is None:
        return "?"
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if abs(n) >= 1_000:
        return f"{n / 1_000:.0f}k"
    return f"{n:.0f}"


def fetch(src: dict) -> list[RawItem]:
    try:
        resp = httpx.get(HUNGERMAP_URL, timeout=60, follow_redirects=True)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        log.warning("HungerMap fetch failed: %s", exc)
        return []

    rows = _extract_rows(payload)
    if not rows:
        keys = list(payload.keys()) if isinstance(payload, dict) else type(payload).__name__
        log.info("HungerMap: empty response (envelope keys: %s)", keys)
        return []

    name_lookup = name_to_iso2_lookup()
    items: list[RawItem] = []
    surfaced, stable = 0, 0
    today_label = datetime.now(timezone.utc).strftime("%d %b %Y")

    with db.connect() as conn:
        seeding = is_baseline_run(conn, SOURCE_ID)
        if seeding:
            log.info("HungerMap: first-ever run — recording baselines silently; "
                     "future runs surface (≥10%% AND ≥100k) OR ≥500k changes only.")
        for row in rows:
            iso = resolve_iso2(row, name_lookup)
            if not iso:
                continue
            count = _extract_count(row)
            if count is None:
                continue

            observed_at = datetime.now(timezone.utc)
            change = record_observation(
                conn, source_id=SOURCE_ID, country_iso2=iso,
                metric_key="insufficient_food_consumption",
                value=count, observed_at=observed_at,
                threshold_rel=HUNGERMAP_REL_THRESHOLD,
                threshold_min_abs_for_rel=HUNGERMAP_MIN_ABS_FOR_REL,
                threshold_abs=HUNGERMAP_ABS_THRESHOLD,
                seed_baseline=seeding,
            )
            if change is None:
                stable += 1
                continue
            surfaced += 1

            country_name = (load_countries()["countries"].get(iso) or {}).get("name") or iso
            # We don't use format_title's numeric branch because we want
            # human-readable units (1.2M people, not 1200000.0/) — build by hand.
            arrow = "↑" if change.direction == "up" else "↓"
            title = (
                f"{change.emoji} [HungerMap] {country_name} food insecurity "
                f"{_fmt_count(change.value_now)} ({arrow} from "
                f"{_fmt_count(change.value_prev)})"
            )
            delta_abs = abs(change.delta or 0)
            rel_pct = (delta_abs / max(abs(change.value_prev or 0), 1)) * 100
            body = (
                f"WFP HungerMap LIVE estimate of people with insufficient food "
                f"consumption in {country_name} moved from "
                f"{_fmt_count(change.value_prev)} to {_fmt_count(change.value_now)} "
                f"({arrow} {_fmt_count(delta_abs)}, {rel_pct:.0f}% change). "
                f"Observed {today_label}."
            )
            items.append(RawItem(
                source_id=src["id"],
                title=title[:300],
                url=f"https://hungermap.wfp.org/?adm0={iso}",
                body=body[:2000],
                published_at=observed_at,
                extra={
                    "hungermap_count": change.value_now,
                    "hungermap_prev": change.value_prev,
                    "hungermap_delta": change.delta,
                },
            ))

    log.info("HungerMap: %d countries surfaced, %d stable", surfaced, stable)
    return items
