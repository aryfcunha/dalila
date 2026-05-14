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
import functools
import io
import logging
import re
import sys
import zipfile
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

import httpx
import yaml

from dalila.models import RawItem, title_case_clean

log = logging.getLogger(__name__)

# Path to the curated outlet allowlist — repo-root YAML, easy to edit
# without touching code. Loaded lazily and cached.
_OUTLETS_YAML = Path(__file__).resolve().parents[3] / "trusted_outlets.yaml"


@functools.lru_cache(maxsize=1)
def _load_trusted_outlets() -> frozenset[str]:
    """Read trusted_outlets.yaml once, return a frozenset of bare domains.

    Missing file → empty set (filter degrades to "accept everything"); logged
    once so it's discoverable but never crashes ingest.
    """
    try:
        with open(_OUTLETS_YAML, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        outlets = data.get("outlets") or []
        # Strip any accidental leading "www." / protocol in the list itself.
        cleaned = {
            re.sub(r"^(https?://)?(www\.)?", "", str(o).strip().lower()).rstrip("/")
            for o in outlets
            if o
        }
        log.info("gdelt: loaded %d trusted outlets from %s",
                 len(cleaned), _OUTLETS_YAML.name)
        return frozenset(cleaned)
    except FileNotFoundError:
        log.warning("gdelt: %s not found — GDELT items will not be domain-filtered",
                    _OUTLETS_YAML)
        return frozenset()
    except Exception as exc:
        log.warning("gdelt: failed to load %s: %s — disabling filter",
                    _OUTLETS_YAML, exc)
        return frozenset()


def _is_trusted_url(url: str, allowlist: frozenset[str]) -> bool:
    """Suffix-match the URL's hostname against the allowlist.

    Empty allowlist passes everything (degrades-open). Otherwise the URL's
    host (minus leading "www.") must equal a listed domain, OR end with
    ".<listed-domain>" (so "english.aawsat.com" matches "aawsat.com").
    """
    if not allowlist:
        return True
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return False
    host = host.lower().lstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return False
    # Walk up the dotted hierarchy: host, then each suffix.
    parts = host.split(".")
    for i in range(len(parts) - 1):
        candidate = ".".join(parts[i:])
        if candidate in allowlist:
            return True
    return False

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

# Acronyms that should stay UPPERCASE even after title-casing the slug.
# Order doesn't matter; we substring-replace word-by-word.
# Lowercase-stays-lowercase (mid-sentence connectors). First word still
# gets capitalised regardless.
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
    allowlist = _load_trusted_outlets()
    # FILTER FIRST, THEN CAP. A 15-min GKG slice has 2,000–5,000 articles
    # in chronological order. The cap used to apply *before* the allowlist,
    # which meant we only looked at the most-recent 300 rows and dropped
    # ~93% of them as regional papers — systematically missing trusted
    # outlets that happened to appear earlier in the slice. Now we walk
    # all rows, keep only allowlisted ones, and apply the cap to that
    # filtered list. The cap exists only to bound runaway volume, not
    # to act as a sampler.
    trusted_rows: list[list[str]] = []
    skipped_outlets = 0
    for row in rows:
        if len(row) < 14:
            continue
        doc_url = row[4].strip()
        if not doc_url or not doc_url.startswith("http"):
            continue
        if not _is_trusted_url(doc_url, allowlist):
            skipped_outlets += 1
            continue
        trusted_rows.append(row)
    log.info("gdelt live: scanned %d rows, %d on allowlist (dropped %d)",
             len(rows), len(trusted_rows), skipped_outlets)
    # Most-recent N from the trusted set
    for row in trusted_rows[-GDELT_MAX_ITEMS_PER_POLL:]:
        date_str = row[1]
        doc_url = row[4].strip()
        themes = row[7]
        persons = row[11]
        orgs = row[13]
        # GDELT doesn't carry article title in GKG. Fabricate a placeholder
        # from the URL slug. title_case_clean strips trailing tracking-hash
        # suffixes (e.g. "...defend-country-ef796d64") and applies readable
        # title-casing with acronym preservation (UAE / US / UN / WFP, etc.).
        slug = doc_url.split("/")[-1] or doc_url
        # Strip path-suffix extensions and split words on dashes / underscores.
        for ext in (".html", ".htm", ".php", ".aspx", ".jsp"):
            if slug.lower().endswith(ext):
                slug = slug[: -len(ext)]
        slug = slug.replace("-", " ").replace("_", " ").strip()
        title_seed = title_case_clean(slug)
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
    log.info("gdelt live: kept %d items", len(items))
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
    allowlist = _load_trusted_outlets()
    # Filter to allowlisted outlets FIRST across the whole slice, then cap.
    # (See the parallel comment in fetch() above.)
    trusted_rows: list[list[str]] = []
    for row in rows:
        if len(row) < 14:
            continue
        doc_url = row[4].strip()
        if not doc_url or not doc_url.startswith("http"):
            continue
        if not _is_trusted_url(doc_url, allowlist):
            continue
        trusted_rows.append(row)
    for row in trusted_rows[-cap:]:
        date_str = row[1]
        doc_url = row[4].strip()
        themes = row[7]
        persons = row[11]
        orgs = row[13]
        # Title synthesis (mirrors the live fetch() path).
        slug = doc_url.split("/")[-1] or doc_url
        for ext in (".html", ".htm", ".php", ".aspx", ".jsp"):
            if slug.lower().endswith(ext):
                slug = slug[: -len(ext)]
        slug = slug.replace("-", " ").replace("_", " ").strip()
        title_seed = title_case_clean(slug)
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
