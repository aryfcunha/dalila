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
import zipfile
from datetime import datetime, timezone

import httpx

from dalila.models import RawItem

log = logging.getLogger(__name__)

LASTUPDATE_URL = "http://data.gdeltproject.org/gdeltv2/lastupdate.txt"

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
    rows = list(reader)
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
