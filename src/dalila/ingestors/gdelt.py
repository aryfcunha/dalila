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
    concurrency: int = 8,
) -> Iterator[RawItem]:
    """Yield RawItems from GDELT GKG slices between since…until.

    `step_minutes=60` samples one slice per hour. With ~24 slices/day and a
    Jan→May span that's ~3,200 zips — serial fetch is ~hour-plus, so we run
    a small ThreadPoolExecutor pool (default 8 workers). Items are yielded
    out-of-order; that's fine because the DB inserts dedupe via url_hash.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    until = until or date.today()
    slice_urls = list(_iter_gkg_slices(since, until, step_minutes=step_minutes))
    total = len(slice_urls)
    log.info("gdelt backfill: %d slices to fetch (step=%dm, concurrency=%d)",
             total, step_minutes, concurrency)

    # Shared client → TCP connection pooling + HTTP/2 (when supported).
    # Drops ~50% of overhead vs httpx.get's per-call client construction.
    # Tight timeouts: GDELT zips are tiny (~1-3MB) and served via CDN; if
    # one hangs past 25s it's effectively dead.
    timeout = httpx.Timeout(connect=10.0, read=25.0, write=10.0, pool=10.0)
    limits = httpx.Limits(max_connections=concurrency * 2, max_keepalive_connections=concurrency)

    def _fetch_and_parse(client: httpx.Client, url: str) -> list[RawItem]:
        try:
            resp = client.get(url)
            if resp.status_code != 200:
                return []
            return _parse_slice(resp.content, source_id, cap=items_per_slice)
        except Exception as exc:
            log.debug("gdelt backfill: %s → %s", url, exc)
            return []

    # Batch submission: process N at a time so progress logs are timely and
    # one slow group can't park 3,000 futures in memory before any complete.
    BATCH = max(concurrency * 4, 40)
    done = 0
    total_yielded = 0
    with httpx.Client(timeout=timeout, limits=limits, follow_redirects=True) as client:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            for batch_start in range(0, total, BATCH):
                batch_urls = slice_urls[batch_start : batch_start + BATCH]
                futures = [pool.submit(_fetch_and_parse, client, u) for u in batch_urls]
                for fut in as_completed(futures):
                    done += 1
                    try:
                        batch = fut.result()
                    except Exception as exc:
                        log.debug("gdelt backfill: worker failed: %s", exc)
                        batch = []
                    for it in batch:
                        total_yielded += 1
                        yield it
                    if done % 20 == 0:
                        log.info("gdelt backfill: %d/%d slices processed, %d items yielded",
                                 done, total, total_yielded)
    log.info("gdelt backfill: %d slices done, %d items total", done, total_yielded)
