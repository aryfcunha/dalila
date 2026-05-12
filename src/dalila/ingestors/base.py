"""Ingestor dispatch and shared helpers."""

from __future__ import annotations

import logging
from typing import Iterator

from dalila.config import load_sources
from dalila.models import RawItem

log = logging.getLogger(__name__)


def iter_enabled_sources() -> Iterator[dict]:
    for src in load_sources():
        if src.get("enabled", True):
            yield src


def ingest_source(src: dict) -> list[RawItem]:
    """Dispatch a source dict to the right ingestor by `kind`."""
    kind = src.get("kind")
    if kind == "rss" or kind == "idmc":
        from dalila.ingestors import rss
        return rss.fetch(src)
    if kind == "scrape":
        from dalila.ingestors import scrape
        return scrape.fetch(src)
    if kind == "gdelt":
        from dalila.ingestors import gdelt
        return gdelt.fetch(src)
    if kind == "acled":
        from dalila.ingestors import acled
        return acled.fetch(src)
    if kind == "iati":
        from dalila.ingestors import iati
        return iati.fetch(src)
    log.warning("unknown source kind=%r for source %s; skipping", kind, src.get("id"))
    return []
