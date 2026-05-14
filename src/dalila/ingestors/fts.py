"""OCHA Financial Tracking Service ingestor.

FTS publishes every recorded humanitarian funding flow (pledge, commitment,
disbursement) against UN-coordinated appeals. The public Key Figures API is
free and unauthenticated:

    https://keyfigures.api.unocha.org/v1/

We pull the most-recent N flows globally, optionally filtered by donor
'United Arab Emirates' or by recipient organisations matching our entity
watchlist. Each flow becomes an item whose title is human-readable and whose
body is a structured summary the classifier's `financial_commitments`
extraction picks up cleanly.

Reality check on first run: the FTS API has changed query parameter names
twice in the last two years. The shape below is current as of 2026-05;
if a call returns 0 results, hit https://keyfigures.api.unocha.org/docs and
reconcile parameter names. The ingestor logs verbose errors on parse
failures rather than swallowing silently.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

from dalila.models import RawItem

log = logging.getLogger(__name__)

# OCHA exposes two FTS APIs:
#   * keyfigures.api.unocha.org/v1  — aggregated key-figure summaries
#   * api.hpc.tools/v2/public       — raw flow records (what we want)
# The keyfigures endpoint rejects `groupby=flow` with HTTP 400 — that was the
# previous fall-through. Switched to the canonical hpc.tools endpoint, which
# uses `planYear` (not `year`) and doesn't need a groupby parameter.
FTS_BASE = "https://api.hpc.tools/v2/public"
DEFAULT_HEADERS = {"User-Agent": "DalilaBot/0.2 (news monitoring)", "Accept": "application/json"}


def fetch(src: dict) -> list[RawItem]:
    """Pull recent humanitarian funding flows.

    Two passes:
      1. Donor-filtered: country=United Arab Emirates — captures UAE outflows.
      2. Global recent: limit=N — captures all major flows; the prefilter and
         classifier decide which mention UAE recipients/partners.

    Dedup happens at insert time via url_hash (we synthesize a stable url
    per FTS flow ID).
    """
    window_days = int(src.get("window_days", 14))
    since = (datetime.now(timezone.utc) - timedelta(days=window_days)).strftime("%Y-%m-%d")

    items: list[RawItem] = []
    seen_ids: set[str] = set()

    # Pass 1 — UAE as donor
    items += _query(src, donor="United Arab Emirates", since=since, seen=seen_ids, label="UAE donor")
    # Pass 2 — global, capped at 50, surfaces all major flows
    items += _query(src, donor=None, since=since, seen=seen_ids, label="global", limit=50)

    return items


def _query(src: dict, *, donor: str | None, since: str, seen: set[str],
           label: str, limit: int = 80) -> list[RawItem]:
    """One API call against api.hpc.tools/v2/public/fts/flow.

    Canonical raw-flow endpoint. Params:
      planYear   — required filter on the plan year (vs the previous `year`)
      limit      — page size
      sourceCountry / destinationCountry / sourceOrganization — filters
    """
    params: dict = {
        "planYear": datetime.now(timezone.utc).year,
        "limit": limit,
    }
    if donor:
        # hpc.tools uses sourceOrganization for donor org names.
        params["sourceOrganization"] = donor

    try:
        resp = httpx.get(
            f"{FTS_BASE}/fts/flow",
            params=params,
            headers=DEFAULT_HEADERS,
            timeout=30,
            follow_redirects=True,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        log.warning("FTS query (%s) failed: %s", label, exc)
        return []

    flows = _extract_flows(payload)
    if not flows:
        log.info("FTS query (%s) returned 0 flows; raw keys=%s",
                 label, list(payload.keys()) if isinstance(payload, dict) else type(payload).__name__)
        return []

    out: list[RawItem] = []
    for flow in flows:
        flow_id = str(flow.get("id") or flow.get("flowId") or "")
        if not flow_id or flow_id in seen:
            continue
        seen.add(flow_id)

        item = _flow_to_raw_item(src["id"], flow_id, flow)
        if item is not None:
            out.append(item)
    log.info("FTS %s: %d flows -> %d items", label, len(flows), len(out))
    return out


def _extract_flows(payload) -> list[dict]:
    """The FTS response wraps flows in different envelopes depending on the
    grouping parameter. Be lenient — try common shapes in order."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("flows", "data", "results", "items"):
        v = payload.get(key)
        if isinstance(v, list):
            return v
    # Sometimes nested: {data: {flows: [...]}} or {meta: {...}, flows: [...]}.
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("flows", "results", "items"):
            v = data.get(key)
            if isinstance(v, list):
                return v
    return []


def _flow_to_raw_item(source_id: str, flow_id: str, flow: dict) -> RawItem | None:
    """Render an FTS flow as a news-shaped item: title + body + canonical URL."""
    donor = _first_org_name(flow, ("sourceObjects", "source")) or "Unknown donor"
    recipient = _first_org_name(flow, ("destinationObjects", "destination")) or "Unknown recipient"
    amount = flow.get("amountUSD") or flow.get("amount_usd") or flow.get("amount")
    status = flow.get("status") or flow.get("contributionType") or "commitment"
    appeal = _first_appeal_name(flow)
    date_str = flow.get("date") or flow.get("createdAt") or flow.get("decisionDate")

    if amount is None:
        return None
    try:
        amount_n = float(amount)
    except (TypeError, ValueError):
        return None

    title = (
        f"FTS: {donor} → {recipient} | "
        f"${amount_n/1_000_000:.1f}M {status}"
    )
    if appeal:
        title += f" ({appeal})"

    body_lines = [
        f"Donor: {donor}",
        f"Recipient: {recipient}",
        f"Amount: USD {amount_n:,.0f}",
        f"Status: {status}",
    ]
    if appeal:
        body_lines.append(f"Appeal: {appeal}")
    if date_str:
        body_lines.append(f"Date: {date_str}")
    if flow.get("description"):
        body_lines.append(f"Description: {str(flow['description'])[:500]}")

    body = "\n".join(body_lines)
    url = f"https://fts.unocha.org/flows/{flow_id}"

    pub: datetime | None = None
    if date_str:
        try:
            pub = datetime.fromisoformat(str(date_str).replace("Z", "+00:00"))
        except Exception:
            pub = None

    return RawItem(
        source_id=source_id,
        title=title[:280],
        url=url,
        body=body,
        published_at=pub,
        extra={"fts_flow_id": flow_id, "amount_usd": amount_n},
    )


def _first_org_name(flow: dict, keys: tuple[str, ...]) -> str | None:
    """Find the first organisation name across alternative FTS field names."""
    for key in keys:
        objs = flow.get(key)
        if isinstance(objs, list):
            for o in objs:
                if isinstance(o, dict):
                    name = o.get("name") or o.get("organisation") or o.get("organization")
                    if name:
                        return str(name)
        elif isinstance(objs, dict):
            name = objs.get("name") or objs.get("organisation")
            if name:
                return str(name)
    return None


def _first_appeal_name(flow: dict) -> str | None:
    for key in ("destinationObjects", "destination"):
        objs = flow.get(key)
        if isinstance(objs, list):
            for o in objs:
                if isinstance(o, dict) and o.get("type") in ("Plan", "Appeal", "Emergency"):
                    return str(o.get("name") or "")
    return None
