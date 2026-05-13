"""GDELT 2.0 ingestor — pulls the latest 15-minute event slice.

GDELT publishes a CSV of global events every 15 minutes. The latest URL is
posted at data.gdeltproject.org/gdeltv2/lastupdate.txt as three lines —
size, hash, url for each of events / mentions / gkg. We use the GKG (Global
Knowledge Graph) export because it carries themes, persons, organizations,
and the source article URL — which is what we actually want.

This ingestor is intentionally narrow: it pulls one slice per poll. With the
default 30-minute poll interval, that means we'll miss one slice in two, which
is fine — GDELT is for *event detection*, not exhaustive coverage. Items have
no body text; the prefilter and classifier work off title + URL only.
"""

from __future__ import annotations

import csv
import io
import logging
import sys
import zipfile
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Iterator

import httpx

from dalila.models import RawItem

log = logging.getLogger(__name__)

# GDELT GKG rows have THEMES / ORGS / PERSONS columns that can easily exceed
# Python's default csv field cap (131072 chars). Raise it to ~10MB once at
# import time; a single GKG zip is <10MB total, so this is a generous ceiling.
try:
    csv.field_size_limit(10_000_000)
except OverflowError:
    csv.field_size_limit(sys.maxsize // 2)

LASTUPDATE_URL = "http://data.gdeltproject.org/gdeltv2/lastupdate.txt"

# GDELT v2 emits one GKG slice every 15 minutes. URL format:
#   http://data.gdeltproject.org/gdeltv2/{YYYYMMDDHHMMSS}.gkg.csv.zip
_GKG_SLICE_URL = "http://data.gdeltproject.org/gdeltv2/{stamp}.gkg.csv.zip"

# Cap how many items we ingest from a single GDELT slice — they can be huge
# and most won't survive the prefilter anyway. Sample the most recent.
GDELT_MAX_ITEMS_PER_POLL = 300


def fetch(src: dict) -> list[RawItem]:
    try:
        index = httpx.get(LASTUPDATE_URL, timeout=20).text.strip().splitlines()
    except Exception as exc:
        log.warning("GDELT lastupdate fetch failed: %s", exc)
        return []

    # Pick the GKG entry (third line). Format: "<size> <hash> <url>"
    gkg_url = None
    for line in index:
        parts = line.split()
        if len(parts) >= 3 and parts[-1].endswith(".gkg.csv.zip"):
            gkg_url = parts[-1]
            break
    if not gkg_url:
        log.warning("GDELT lastupdate did not contain a GKG entry; got %r", index)
        return []

    try:
        zip_bytes = httpx.get(gkg_url, timeout=60).content
    except Exception as exc:
        log.warning("GDELT GKG download failed: %s", exc)
        return []

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            csv_name = zf.namelist()[0]
            with zf.open(csv_name) as f:
                text = f.read().decode("utf-8", errors="replace")
    except Exception as exc:
        log.warning("GDELT GKG zip parse failed: %s", exc)
        return []

    # GKG schema: see http://data.gdeltproject.org/documentation/GDELT-Global_Knowledge_Graph_Codebook-V2.1.pdf
    # We need: V2.1DATE (col 1), DocumentIdentifier (URL, col 4), V2THEMES (col 7),
    # V2LOCATIONS (col 9), V2PERSONS (col 11), V2ORGANIZATIONS (col 13), V2.1EXTRASXML for title
    items: list[RawItem] = []
    reader = csv.reader(io.StringIO(text), delimiter="\t", quoting=csv.QUOTE_NONE)
    rows: list[list[str]] = []
    while True:
        try:
            row = next(reader)
        except StopIteration:
            break
        except (csv.Error, Exception) as exc:
            log.debug("gdelt: skipping bad CSV row: %s", exc)
            continue
        rows.append(row)
    # Sample most recent (rows are chronological — take last N)
    for row in rows[-GDELT_MAX_ITEMS_PER_POLL:]:
        if len(row) < 14:
            continue
        date_str = row[1]
        doc_url = row[4].strip()
        themes = row[7]
        persons = row[11]
        orgs = row[13]

        if not doc_url or not doc_url.startswith("http"):
            continue
        # GDELT doesn't carry article title in GKG. Fabricate a placeholder
        # from URL + themes; classifier will work off URL + themes for prefilter.
        title_seed = doc_url.split("/")[-1] or doc_url
        title_seed = title_seed.replace("-", " ").replace("_", " ").rstrip(".html").strip()
        if not title_seed:
            continue
        body_parts = []
        if themes:
            body_parts.append("Themes: " + themes[:500])
        if orgs:
            body_parts.append("Orgs: " + orgs[:300])
        if persons:
            body_parts.append("Persons: " + persons[:300])
        body = " | ".join(body_parts) or None

        try:
            published_at = datetime.strptime(date_str[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except Exception:
            published_at = None

        items.append(RawItem(
            source_id=src["id"],
            title=title_seed[:200],
            url=doc_url,
            body=body,
            published_at=published_at,
            extra={"gdelt_themes": themes, "gdelt_orgs": orgs},
        ))
    return items


# ---------------------------------------------------------------------------
# Historical backfill — walk 15-minute slices over a date range.
# ---------------------------------------------------------------------------


def _iter_gkg_slices(since: date, until: date, step_minutes: int = 60) -> Iterator[str]:
    """Yield GKG zip URLs for each 15-min slice between since and until.

    `step_minutes` lets us sub-sample (default: one slice per hour, not all
    four). GDELT's volume is enormous; sampling 1/4 of slices still leaves us
    with thousands of items per day after the prefilter.
    """
    start = datetime.combine(since, dtime.min, tzinfo=timezone.utc)
    end = datetime.combine(until, dtime.max, tzinfo=timezone.utc)
    cur = start
    delta = timedelta(minutes=step_minutes)
    while cur <= end:
        # GDELT slices are emitted at :00, :15, :30, :45 — round down to :15.
        mm = (cur.minute // 15) * 15
        stamp = cur.replace(minute=mm, second=0, microsecond=0).strftime("%Y%m%d%H%M%S")
        yield _GKG_SLICE_URL.format(stamp=stamp)
        cur = cur + delta


def _parse_slice(zip_bytes: bytes, source_id: str, *, cap: int) -> list[RawItem]:
    """Reuse the same row-extraction logic as fetch() but over a given zip."""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            csv_name = zf.namelist()[0]
            with zf.open(csv_name) as f:
                text = f.read().decode("utf-8", errors="replace")
    except Exception as exc:
        log.debug("gdelt slice parse failed: %s", exc)
        return []
    items: list[RawItem] = []
    reader = csv.reader(io.StringIO(text), delimiter="\t", quoting=csv.QUOTE_NONE)
    # Iterate row-by-row inside try so one oversized field doesn't lose the
    # whole slice. The 10MB field-size ceiling already covers normal rows.
    rows: list[list[str]] = []
    while True:
        try:
            row = next(reader)
        except StopIteration:
            break
        except (csv.Error, Exception) as exc:
            log.debug("gdelt: skipping bad CSV row: %s", exc)
            continue
        rows.append(row)
    for row in rows[-cap:]:
        if len(row) < 14:
            continue
        date_str = row[1]
        doc_url = row[4].strip()
        themes = row[7]
        persons = row[11]
        orgs = row[13]
        if not doc_url or not doc_url.startswith("http"):
            continue
        title_seed = doc_url.split("/")[-1] or doc_url
        title_seed = title_seed.replace("-", " ").replace("_", " ").rstrip(".html").strip()
        if not title_seed:
            continue
        body_parts = []
        if themes:
            body_parts.append("Themes: " + themes[:500])
        if orgs:
            body_parts.append("Orgs: " + orgs[:300])
        if persons:
            body_parts.append("Persons: " + persons[:300])
        body = " | ".join(body_parts) or None
        try:
            published_at = datetime.strptime(date_str[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except Exception:
            published_at = None
        items.append(RawItem(
            source_id=source_id,
            title=title_seed[:200],
            url=doc_url,
            body=body,
            published_at=published_at,
            extra={"gdelt_themes": themes, "gdelt_orgs": orgs},
        ))
    return items


def iter_items_range(
    source_id: str,
    since: date,
    until: date | None = None,
    *,
    step_minutes: int = 60,
    items_per_slice: int = 60,
) -> Iterator[RawItem]:
    """Yield RawItems from GDELT GKG slices between since…until.

    `step_minutes=60` samples one slice per hour (vs four). The default
    `items_per_slice=60` further down-samples each slice — the prefilter is
    where the real culling happens; we just need enough volume that genuine
    UAE/humanitarian stories are statistically guaranteed to surface.
    """
    until = until or date.today()
    for url in _iter_gkg_slices(since, until, step_minutes=step_minutes):
        try:
            resp = httpx.get(url, timeout=60)
            if resp.status_code != 200:
                continue
            zip_bytes = resp.content
        except Exception as exc:
            log.debug("gdelt backfill: %s → %s", url, exc)
            continue
        for it in _parse_slice(zip_bytes, source_id, cap=items_per_slice):
            yield it
