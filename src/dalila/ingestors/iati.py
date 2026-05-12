"""IATI Datastore ingestor — pulls UAE-related aid activities.

The International Aid Transparency Initiative (IATI) Datastore aggregates
structured activity reporting from donors and recipients. UAE entities
publishing to IATI include Abu Dhabi Fund for Development and UAE Aid
Agency; many programmes are also reported by recipient countries or
implementing partners with UAE listed as donor.

We query the Datastore Solr API for activities where:
  - reporting-org country = UAE (donor reporting)
  - OR participating-org country includes UAE (recipient/partner reporting)
in a recent activity-date window. Each matching activity becomes a RawItem
whose body contains the activity title + description + commitment amounts.

Compared to RSS/scrape, IATI is the only ingestor that yields *structured*
financial commitment data. The classifier's financial_commitments
extraction picks that up naturally because the amounts are written into
the body text. A future v0.2 could short-circuit and write directly to
financial_commitments without going through the classifier — but that
breaks the uniform ingestor abstraction, so we don't.

API docs: https://iatistandard.org/en/iati-tools-and-resources/the-iati-datastore/
The Datastore is free, requires no key, but is occasionally flaky — failures
are logged and return [] rather than raising (matches other ingestors).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

from dalila.config import get_config
from dalila.models import RawItem

log = logging.getLogger(__name__)

_DATASTORE = "https://api.iatistandard.org/datastore/activity/select"

# Fields we pull from each activity. Kept minimal — extra fields just bloat
# the response and we'd ignore them anyway.
_FIELDS = ",".join([
    "iati_identifier",
    "title_narrative",
    "description_narrative",
    "reporting_org_narrative",
    "reporting_org_ref",
    "activity_date_iso_date",
    "activity_date_type",
    "transaction_value",
    "transaction_value_currency",
    "transaction_type_code",
    "recipient_country_code",
    "recipient_country_narrative",
])


def _build_query(window_days: int) -> str:
    """Solr `q` for activities tagged to UAE (donor or participating-org).

    UAE codes in IATI: country `AE`, and well-known publisher reference
    prefixes for ADFD (`XM-OB-ADFD`) and UAE Aid Agency (registry IDs vary).
    Filtering by reporting_org_ref:*UAE* is too lossy because UAE publishers
    don't all use a UAE country code; the participating_org and recipient
    country fields capture the broader relevant set.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).strftime("%Y-%m-%d")
    # Solr OR for: UAE as participating org country, UAE as recipient,
    # or reporting-org narrative mentions Emirates / ADFD / Khalifa.
    return (
        f"activity_date_iso_date:[{cutoff} TO *] AND ("
        f"  participating_org_narrative:(*Emirates* OR *UAE* OR *ADFD* OR *Khalifa*)"
        f"  OR reporting_org_narrative:(*Emirates* OR *UAE* OR *ADFD* OR *Khalifa* OR *Erth Zayed*)"
        f")"
    ).replace("  ", " ")


def fetch(src: dict) -> list[RawItem]:
    cfg = get_config()
    if not cfg.iati_api_key:
        # Same silent-skip pattern as ACLED — the source can stay enabled
        # in sources.yaml and just produce zero items until a key is set.
        log.info("iati skipped: IATI_API_KEY not set in .env")
        return []
    window_days = int(src.get("window_days", 30))
    rows = int(src.get("rows", 50))
    try:
        resp = httpx.get(
            _DATASTORE,
            params={
                "q": _build_query(window_days),
                "fl": _FIELDS,
                "rows": rows,
                "wt": "json",
                "sort": "activity_date_iso_date desc",
            },
            timeout=30,
            headers={
                "Accept": "application/json",
                "Ocp-Apim-Subscription-Key": cfg.iati_api_key,
            },
        )
        resp.raise_for_status()
    except Exception as exc:
        log.warning("iati datastore fetch failed: %s", exc)
        return []

    try:
        data = resp.json()
    except Exception as exc:
        log.warning("iati datastore returned non-JSON: %s", exc)
        return []

    docs = (data.get("response") or {}).get("docs") or []
    items: list[RawItem] = []
    for d in docs:
        iati_id = _first(d.get("iati_identifier")) or ""
        title = _first(d.get("title_narrative")) or iati_id
        description = _join_narratives(d.get("description_narrative"))
        reporter = _first(d.get("reporting_org_narrative")) or "(reporter unknown)"
        date_iso = _first(d.get("activity_date_iso_date"))
        recipient = _join_narratives(d.get("recipient_country_narrative"))

        # Build a body that the classifier's financial-commitment extractor
        # will recognise. Include any transaction amounts as a "Commitment: X
        # CUR" line so the LLM can parse them out.
        body_parts = [
            description[:1500],
            f"Reporting organisation: {reporter}",
        ]
        if recipient:
            body_parts.append(f"Recipient: {recipient}")
        amounts = _format_amounts(d)
        if amounts:
            body_parts.append("Reported commitments:")
            body_parts.extend(f"  - {a}" for a in amounts)
        body = "\n".join(p for p in body_parts if p)

        # Use the IATI activity URL on d-portal for the link — IATI itself
        # has no human-readable page, but d-portal mirrors the registry.
        url = f"https://d-portal.org/q.html?aid={iati_id}" if iati_id else None
        items.append(RawItem(
            source_id=src["id"],
            title=title[:300],
            url=url,
            body=body or None,
            author=reporter,
            published_at=_parse_date(date_iso),
        ))
    log.info("iati: pulled %d activities", len(items))
    return items


def _first(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, list):
        return v[0] if v else None
    return str(v)


def _join_narratives(v) -> str:
    if not v:
        return ""
    if isinstance(v, list):
        return " · ".join(str(x).strip() for x in v if str(x).strip())
    return str(v).strip()


def _format_amounts(d: dict) -> list[str]:
    """Pair up parallel transaction arrays into 'Commitment: 1.2M USD' lines."""
    vals = d.get("transaction_value") or []
    curs = d.get("transaction_value_currency") or []
    typs = d.get("transaction_type_code") or []
    if not isinstance(vals, list):
        return []
    out: list[str] = []
    for i, v in enumerate(vals):
        cur = (curs[i] if i < len(curs) else "") or "?"
        typ = (typs[i] if i < len(typs) else "") or "?"
        try:
            amt = float(v)
        except (TypeError, ValueError):
            continue
        # IATI transaction type codes: 2=outgoing commitment, 3=disbursement
        typ_label = {"2": "Commitment", "3": "Disbursement", "1": "Incoming"}.get(str(typ), "Transaction")
        out.append(f"{typ_label}: {amt:,.0f} {cur}")
    return out[:5]


def _parse_date(s: str | None):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s[:10]).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
