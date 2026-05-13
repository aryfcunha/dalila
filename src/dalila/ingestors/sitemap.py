"""Sitemap-walking backfill scraper.

Why this exists: RSS feeds only carry the most-recent 20-50 items. To backfill
historical content (Jan 1 2026 → today) for WAM, The National, and Gulf News
we walk their public XML sitemaps instead. Sitemaps are explicitly designed
for crawlers, return static XML (no JS rendering needed), and are gzip-friendly.

Three URL patterns are supported:

1. `monthly_template` — e.g. `https://wam.ae/sitemap/news/en/{year}/{month}/english.xml`
   We expand `{year}` / `{month}` for every month from `since` to today.

2. `index_url` — a sitemap-index XML whose `<sitemap><loc>` entries point to
   per-day or per-month child sitemaps. We fetch the index, filter children
   by `<lastmod>` >= `since` (if present), then walk each child.

3. `daily_template` — e.g. `https://example.com/sitemap-{year}-{month}-{day}.xml`
   We expand for every day in the window.

Per child sitemap we extract `<url><loc>` entries (optionally filtered by
`<lastmod>`), then fetch each article HTML to pull a title + meta-description
body. The body is short (only what's in `<meta name="description">` or the
opening paragraph) — full-article extraction is intentionally out of scope to
keep cost bounded; the classifier doesn't need the whole article anyway.

Polite scraping: 0.3s sleep between article fetches, 30s timeout, MAX_PER_DAY
cap (default 80) to prevent runaway crawls. Failures are logged and skipped,
not raised — a backfill that hits one dead month should still complete the
others.
"""

from __future__ import annotations

import gzip
import io
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

import httpx
from bs4 import BeautifulSoup

from dalila.models import RawItem

log = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; DalilaBot/0.1; +personal use; news monitoring)"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# XML namespace used by every sitemap protocol implementation.
_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _http_get(url: str, *, timeout: int = 30) -> bytes | None:
    try:
        resp = httpx.get(url, headers=DEFAULT_HEADERS, timeout=timeout, follow_redirects=True)
        if resp.status_code != 200:
            log.debug("sitemap: %s → HTTP %d", url, resp.status_code)
            return None
        return resp.content
    except Exception as exc:
        log.debug("sitemap: %s → %s", url, exc)
        return None


def _maybe_gunzip(blob: bytes) -> bytes:
    """Sitemaps are often served as .xml.gz. Detect by magic header."""
    if blob[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(blob)
        except Exception:
            return blob
    return blob


# ---------------------------------------------------------------------------
# Sitemap XML parsing
# ---------------------------------------------------------------------------


def _parse_xml(blob: bytes) -> ET.Element | None:
    try:
        return ET.fromstring(_maybe_gunzip(blob))
    except ET.ParseError as exc:
        log.debug("sitemap: XML parse failed: %s", exc)
        return None


def _parse_lastmod(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    # Handle date-only, full ISO with Z, and full ISO with offset.
    try:
        if len(s) == 10:
            return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        s2 = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s2)
    except Exception:
        return None


def _extract_urls_from_sitemap(root: ET.Element, since_dt: datetime) -> list[tuple[str, datetime | None]]:
    """Pull `<url>` entries (filtered by lastmod >= since when present)."""
    out: list[tuple[str, datetime | None]] = []
    # Sitemaps may declare any namespace prefix; iterate by local-name.
    for url_el in root.iter("{http://www.sitemaps.org/schemas/sitemap/0.9}url"):
        loc = url_el.findtext("sm:loc", default="", namespaces=_NS).strip()
        if not loc:
            continue
        lm = _parse_lastmod(url_el.findtext("sm:lastmod", default="", namespaces=_NS))
        if lm and lm < since_dt:
            continue
        out.append((loc, lm))
    return out


def _extract_child_sitemaps(root: ET.Element, since_dt: datetime) -> list[str]:
    """Pull `<sitemap><loc>` from a sitemap-index (filtered by lastmod)."""
    out: list[str] = []
    for sm in root.iter("{http://www.sitemaps.org/schemas/sitemap/0.9}sitemap"):
        loc = sm.findtext("sm:loc", default="", namespaces=_NS).strip()
        if not loc:
            continue
        lm = _parse_lastmod(sm.findtext("sm:lastmod", default="", namespaces=_NS))
        if lm and lm < since_dt:
            continue
        out.append(loc)
    return out


# ---------------------------------------------------------------------------
# Article HTML extraction
# ---------------------------------------------------------------------------


_DATE_IN_URL_RE = re.compile(r"/(\d{4})[/-](\d{1,2})[/-](\d{1,2})/")


def _date_from_url(url: str) -> datetime | None:
    m = _DATE_IN_URL_RE.search(url)
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
    except Exception:
        return None


def _extract_article(html: bytes, url: str) -> tuple[str, str | None, datetime | None]:
    """Pull (title, body, published_at) from an article page.

    Body is whatever we can scrape cheaply: meta description, og:description,
    or the first paragraph. We do NOT try to reconstruct full article text —
    that's a different (and brittle) project. The classifier's contract only
    requires title + a few sentences of context.
    """
    soup = BeautifulSoup(html, "lxml")

    def _meta(name: str, attr: str = "name") -> str | None:
        el = soup.find("meta", attrs={attr: name})
        if el and el.get("content"):
            return el["content"].strip() or None
        return None

    title = (
        _meta("og:title", "property")
        or _meta("twitter:title")
        or (soup.title.get_text().strip() if soup.title else None)
        or ""
    )
    title = title.strip()
    if not title:
        return "", None, None

    body = (
        _meta("description")
        or _meta("og:description", "property")
        or _meta("twitter:description")
    )
    if not body:
        # Fall back to first non-trivial paragraph.
        for p in soup.find_all("p"):
            txt = p.get_text(separator=" ", strip=True)
            if txt and len(txt) > 60:
                body = txt[:1500]
                break
    body = (body or "").strip() or None

    # Try article-published-time meta first, then date in URL.
    pub_raw = (
        _meta("article:published_time", "property")
        or _meta("article:published_time")
        or _meta("pubdate")
        or _meta("og:published_time", "property")
    )
    pub_dt: datetime | None = None
    if pub_raw:
        pub_dt = _parse_lastmod(pub_raw)
    if pub_dt is None:
        pub_dt = _date_from_url(url)

    return title, body, pub_dt


# ---------------------------------------------------------------------------
# Backfill source registry
# ---------------------------------------------------------------------------


# Hard-coded so a malformed sources.yaml can't break the backfill. These
# are not the normal ingest sources — they're parallel "historical" probes
# of the same publishers we already track via RSS.
BACKFILL_SOURCES: dict[str, dict] = {
    "wam_en": {
        "name": "Emirates News Agency (WAM)",
        "monthly_template": "https://wam.ae/sitemap/news/en/{year}/{month}/english.xml",
        "tags": ["uae", "state", "wire"],
        "fetch_body": True,
    },
    "the_national": {
        "name": "The National",
        # Arc Publishing exposes a sitemap-index per year/month.
        "index_url": "https://www.thenationalnews.com/arc/outboundfeeds/sitemap-index/",
        "tags": ["uae", "press"],
        "fetch_body": True,
    },
    "gulf_news": {
        "name": "Gulf News",
        # Top-level sitemap-index — walks down to dated child sitemaps.
        "index_url": "https://gulfnews.com/sitemap.xml",
        "tags": ["uae", "gulf", "press"],
        "fetch_body": True,
    },
}


# ---------------------------------------------------------------------------
# Public API: yield items for one backfill source between since…until
# ---------------------------------------------------------------------------


def iter_items(
    source_id: str,
    since: date,
    until: date | None = None,
    *,
    max_per_source: int = 4000,
    article_sleep_s: float = 0.3,
):
    """Yield RawItems for `source_id` between `since` and `until` (inclusive).

    This is a generator so callers (pipeline.run_backfill) can persist items
    as they arrive and surface progress without buffering the whole run.
    """
    src = BACKFILL_SOURCES.get(source_id)
    if src is None:
        log.warning("backfill: no recipe for source_id=%r", source_id)
        return
    until = until or date.today()
    since_dt = datetime(since.year, since.month, since.day, tzinfo=timezone.utc)

    # Step 1: collect candidate sitemap URLs by walking templates / indexes.
    sitemap_urls = list(_resolve_sitemaps(src, since, until, since_dt))
    log.info(
        "backfill[%s]: resolved %d sitemap URL(s) for %s → %s",
        source_id, len(sitemap_urls), since, until,
    )

    # Step 2: from each sitemap, gather article URLs + lastmod.
    seen_urls: set[str] = set()
    article_urls: list[tuple[str, datetime | None]] = []
    for sm_url in sitemap_urls:
        blob = _http_get(sm_url)
        if blob is None:
            continue
        root = _parse_xml(blob)
        if root is None:
            continue
        # A sitemap can itself be a nested index — handle one level of nesting.
        nested = _extract_child_sitemaps(root, since_dt)
        if nested:
            for n in nested[:50]:
                blob2 = _http_get(n)
                if blob2 is None:
                    continue
                root2 = _parse_xml(blob2)
                if root2 is None:
                    continue
                for u, lm in _extract_urls_from_sitemap(root2, since_dt):
                    if u in seen_urls:
                        continue
                    seen_urls.add(u)
                    article_urls.append((u, lm))
        else:
            for u, lm in _extract_urls_from_sitemap(root, since_dt):
                if u in seen_urls:
                    continue
                seen_urls.add(u)
                article_urls.append((u, lm))
        if len(article_urls) >= max_per_source:
            log.info("backfill[%s]: hit max_per_source=%d cap during URL collection",
                     source_id, max_per_source)
            break

    log.info("backfill[%s]: %d candidate article URLs gathered", source_id, len(article_urls))

    # Step 3: fetch each article HTML and yield a RawItem.
    fetched = 0
    for url, lm in article_urls[:max_per_source]:
        if src.get("fetch_body", True):
            blob = _http_get(url)
            if blob is None:
                continue
            title, body, pub_dt = _extract_article(blob, url)
            time.sleep(article_sleep_s)
        else:
            title, body, pub_dt = "", None, None
            # Try to recover something from the URL itself
            slug = url.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").replace("_", " ")
            title = slug[:200]
        if not title:
            continue
        published_at = pub_dt or lm or _date_from_url(url)
        # Skip articles outside the requested window (sitemaps sometimes lie).
        if published_at and published_at < since_dt:
            continue
        yield RawItem(
            source_id=source_id,
            title=title[:300],
            url=url,
            body=body[:2000] if body else None,
            published_at=published_at,
        )
        fetched += 1
    log.info("backfill[%s]: yielded %d items", source_id, fetched)


def _resolve_sitemaps(src: dict, since: date, until: date, since_dt: datetime):
    """Expand `monthly_template` / `daily_template` or walk `index_url`."""
    if tmpl := src.get("monthly_template"):
        cur = date(since.year, since.month, 1)
        end = date(until.year, until.month, 1)
        while cur <= end:
            yield tmpl.format(year=cur.year, month=cur.month)
            # advance one month
            if cur.month == 12:
                cur = date(cur.year + 1, 1, 1)
            else:
                cur = date(cur.year, cur.month + 1, 1)
        return

    if tmpl := src.get("daily_template"):
        cur = since
        while cur <= until:
            yield tmpl.format(year=cur.year, month=cur.month, day=cur.day)
            cur = cur + timedelta(days=1)
        return

    if idx := src.get("index_url"):
        blob = _http_get(idx)
        if blob is None:
            log.warning("backfill: index_url unreachable: %s", idx)
            return
        root = _parse_xml(blob)
        if root is None:
            return
        # An index can be a sitemap-index (children are sitemaps) or a flat
        # sitemap (children are articles). Handle both transparently.
        children = _extract_child_sitemaps(root, since_dt)
        if children:
            yield from children
        else:
            # Flat sitemap — yield the index URL itself so iter_items walks it.
            yield idx
        return
