"""ACLED CAST (Conflict Alert System) ingestor.

CAST is ACLED's monthly conflict-escalation forecast — a 0–10 risk score for
~190 countries, projecting violence trajectories 1–3 months ahead. Updated
the first week of each month.

Auth: piggybacks on the OAuth flow already wired in `acled.py` — same
ACLED_USERNAME + ACLED_PASSWORD env vars, same 24h Bearer token.

Change-detection: we only emit an item when a country's score moves by at
least CAST_DELTA_THRESHOLD (0.5 on the 0–10 scale, ~5%) compared to the
previous monthly observation. Stable scores are recorded silently to the
forecast_snapshots table so the next observation has a baseline, but they
don't reach the brief — "Burkina Faso still high risk" isn't news.

Title format: "🔴 [CAST May 2026] Burkina Faso escalation risk 8.4/10 (↑ from 7.1)".

CAUTION: ACLED hasn't published a fully stable schema for the CAST endpoint
public response shape (as of 2026-05). The field names below (`country`,
`iso3`, `forecast_month`, `cast_score`) are inferred from their tutorial
notebooks; if the real response uses different keys, adjust the field
extraction in `_extract_rows()`. The OAuth flow itself is identical to
acled/read so authentication won't be the failure mode.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from dalila import db
from dalila.config import get_config, load_countries
from dalila.ingestors.acled import _auth_headers
from dalila.ingestors.forecast import format_title, record_observation
from dalila.models import RawItem

log = logging.getLogger(__name__)

CAST_URL = "https://acleddata.com/api/cast/read"
CAST_DELTA_THRESHOLD = 0.5     # 0–10 scale; ~5% of full range
SOURCE_ID = "cast"

# ISO-3 → ISO-2 fallback for when the API returns alpha-3 country codes.
# Mirrors the table in html_digest.py's world map but inverted; small enough
# that duplicating it is cheaper than refactoring into a shared module.
_A3_TO_A2 = {
    "AFG":"AF","ALB":"AL","DZA":"DZ","AGO":"AO","ARG":"AR","ARM":"AM","AUS":"AU",
    "AUT":"AT","AZE":"AZ","BGD":"BD","BLR":"BY","BEL":"BE","BEN":"BJ","BTN":"BT",
    "BOL":"BO","BIH":"BA","BWA":"BW","BRA":"BR","BGR":"BG","BFA":"BF","BDI":"BI",
    "KHM":"KH","CMR":"CM","CAN":"CA","CAF":"CF","TCD":"TD","CHL":"CL","CHN":"CN",
    "COL":"CO","COM":"KM","COG":"CG","COD":"CD","CRI":"CR","CIV":"CI","HRV":"HR",
    "CUB":"CU","CYP":"CY","CZE":"CZ","DNK":"DK","DJI":"DJ","DOM":"DO","ECU":"EC",
    "EGY":"EG","SLV":"SV","GNQ":"GQ","ERI":"ER","EST":"EE","ETH":"ET","FJI":"FJ",
    "FIN":"FI","FRA":"FR","GAB":"GA","GMB":"GM","GEO":"GE","DEU":"DE","GHA":"GH",
    "GRC":"GR","GTM":"GT","GIN":"GN","GNB":"GW","GUY":"GY","HTI":"HT","HND":"HN",
    "HUN":"HU","ISL":"IS","IND":"IN","IDN":"ID","IRN":"IR","IRQ":"IQ","IRL":"IE",
    "ISR":"IL","ITA":"IT","JAM":"JM","JPN":"JP","JOR":"JO","KAZ":"KZ","KEN":"KE",
    "PRK":"KP","KOR":"KR","KWT":"KW","KGZ":"KG","LAO":"LA","LVA":"LV","LBN":"LB",
    "LSO":"LS","LBR":"LR","LBY":"LY","LTU":"LT","LUX":"LU","MDG":"MG","MWI":"MW",
    "MYS":"MY","MDV":"MV","MLI":"ML","MLT":"MT","MRT":"MR","MUS":"MU","MEX":"MX",
    "MDA":"MD","MNG":"MN","MNE":"ME","MAR":"MA","MOZ":"MZ","MMR":"MM","NAM":"NA",
    "NPL":"NP","NLD":"NL","NZL":"NZ","NIC":"NI","NER":"NE","NGA":"NG","MKD":"MK",
    "NOR":"NO","OMN":"OM","PAK":"PK","PSE":"PS","PAN":"PA","PNG":"PG","PRY":"PY",
    "PER":"PE","PHL":"PH","POL":"PL","PRT":"PT","QAT":"QA","ROU":"RO","RUS":"RU",
    "RWA":"RW","SAU":"SA","SEN":"SN","SRB":"RS","SLE":"SL","SGP":"SG","SVK":"SK",
    "SVN":"SI","SOM":"SO","ZAF":"ZA","SSD":"SS","ESP":"ES","LKA":"LK","SDN":"SD",
    "SWE":"SE","CHE":"CH","SYR":"SY","TWN":"TW","TJK":"TJ","TZA":"TZ","THA":"TH",
    "TLS":"TL","TGO":"TG","TUN":"TN","TUR":"TR","TKM":"TM","UGA":"UG","UKR":"UA",
    "ARE":"AE","GBR":"GB","USA":"US","URY":"UY","UZB":"UZ","VEN":"VE","VNM":"VN",
    "YEM":"YE","ZMB":"ZM","ZWE":"ZW",
}


def _name_to_iso2() -> dict[str, str]:
    """Build a lowercase-name → ISO-2 lookup from countries.yaml + aliases."""
    out: dict[str, str] = {}
    cat = load_countries()
    for iso, spec in cat.get("countries", {}).items():
        if not isinstance(spec, dict):
            continue
        name = (spec.get("name") or "").strip().lower()
        if name:
            out[name] = iso
        for alias in (spec.get("aliases") or []):
            out[str(alias).strip().lower()] = iso
    return out


def _resolve_iso2(row: dict, name_lookup: dict[str, str]) -> str | None:
    """Try alpha-3, then alpha-2 (rare), then country name."""
    a3 = (row.get("iso3") or row.get("country_iso3") or "").strip().upper()
    if a3 and len(a3) == 3 and a3 in _A3_TO_A2:
        return _A3_TO_A2[a3]
    a2 = (row.get("iso2") or row.get("country_iso") or "").strip().upper()
    if len(a2) == 2 and a2.isalpha():
        return a2
    name = (row.get("country") or row.get("country_name") or "").strip().lower()
    if name in name_lookup:
        return name_lookup[name]
    return None


def _extract_rows(payload: dict) -> list[dict]:
    """Tolerant extractor — ACLED wraps results under `data` for /acled/read;
    /cast/read is expected to follow the same envelope."""
    if isinstance(payload, dict):
        data = payload.get("data") or payload.get("cast") or []
        if isinstance(data, list):
            return data
    if isinstance(payload, list):
        return payload
    return []


def _forecast_month_label(row: dict) -> str:
    """Best-effort 'May 2026' label from the row's forecast month field."""
    raw = (
        row.get("forecast_month")
        or row.get("month")
        or row.get("forecast_date")
        or ""
    )
    if not raw:
        return datetime.now(timezone.utc).strftime("%b %Y")
    try:
        # Accept YYYY-MM, YYYY-MM-DD, or ISO datetime
        s = str(raw)[:7]
        return datetime.strptime(s, "%Y-%m").strftime("%b %Y")
    except Exception:
        return str(raw)


def fetch(src: dict) -> list[RawItem]:
    cfg = get_config()
    if not cfg.acled_username or not cfg.acled_password:
        log.info("CAST skipped: ACLED_USERNAME / ACLED_PASSWORD not set")
        return []
    headers = _auth_headers()
    if not headers:
        log.info("CAST skipped: could not mint oauth token")
        return []

    try:
        resp = httpx.get(
            CAST_URL,
            params={"_format": "json", "limit": 500},
            headers=headers,
            timeout=60,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        log.warning("CAST fetch failed: %s", exc)
        return []

    rows = _extract_rows(payload)
    if not rows:
        log.info("CAST: empty response — check schema (got keys: %s)",
                 list(payload.keys()) if isinstance(payload, dict) else type(payload))
        return []

    name_lookup = _name_to_iso2()
    items: list[RawItem] = []
    surfaced = 0
    stable = 0

    with db.connect() as conn:
        for row in rows:
            iso = _resolve_iso2(row, name_lookup)
            if not iso:
                continue
            score_raw = (
                row.get("cast_score")
                or row.get("risk_score")
                or row.get("score")
                or row.get("forecast_score")
            )
            try:
                score = float(score_raw) if score_raw is not None else None
            except (TypeError, ValueError):
                score = None
            if score is None:
                continue

            month_label = _forecast_month_label(row)
            observed_at = datetime.now(timezone.utc)
            notes = (row.get("notes") or row.get("description") or "").strip()[:1500] or None

            change = record_observation(
                conn,
                source_id=SOURCE_ID,
                country_iso2=iso,
                metric_key="cast_score",
                value=score,
                observed_at=observed_at,
                threshold_abs=CAST_DELTA_THRESHOLD,
                notes=notes,
            )
            if change is None:
                stable += 1
                continue
            surfaced += 1

            country_name = (load_countries()["countries"].get(iso) or {}).get("name") or iso
            title = format_title(
                change,
                source_label=f"CAST {month_label}",
                country_name=country_name,
                metric_label="escalation risk",
                units="/10",
            )
            body_parts = []
            if change.value_prev is not None:
                body_parts.append(
                    f"ACLED CAST forecast for {country_name} moved from "
                    f"{change.value_prev:.1f} to {change.value_now:.1f} "
                    f"({'↑' if change.direction == 'up' else '↓'}{abs(change.delta or 0):.1f}) "
                    f"on the 0-10 conflict-escalation risk scale, "
                    f"forecast horizon {month_label}."
                )
            else:
                body_parts.append(
                    f"ACLED CAST began tracking {country_name} at "
                    f"{change.value_now:.1f}/10 escalation risk, {month_label}."
                )
            if notes:
                body_parts.append(f"ACLED note: {notes}")

            items.append(RawItem(
                source_id=src["id"],
                title=title[:300],
                url=f"https://acleddata.com/cast/{iso.lower()}",
                body=" ".join(body_parts)[:2000],
                published_at=observed_at,
                extra={"cast_score": change.value_now, "cast_delta": change.delta},
            ))

    log.info("CAST: %d countries surfaced (changed by ≥%.1f), %d stable",
             surfaced, CAST_DELTA_THRESHOLD, stable)
    return items
