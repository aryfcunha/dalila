"""Dataclasses for items, classification results, etc."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class RawItem:
    """An item as pulled from a source, before being persisted."""
    source_id: str
    title: str
    url: str | None
    body: str | None
    author: str | None = None
    published_at: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# Capital-signal flag names. Defined once so we validate consistently in
# both classifier output handling and DB queries.
CAPITAL_SIGNAL_KEYS = (
    "mentions_blended_finance",
    "mentions_debt_swap",
    "mentions_first_loss",
    "mentions_guarantee",
    "mentions_results_based",
)


@dataclass
class FinancialCommitment:
    amount: float | None
    currency: str | None
    fund_name: str | None
    recipient: str | None
    commitment_type: str | None     # pledge | disbursement | mou | loan | grant
    announced_at: str | None
    rationale: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "FinancialCommitment":
        amt = d.get("amount")
        try:
            amt = float(amt) if amt is not None else None
        except (TypeError, ValueError):
            amt = None
        return cls(
            amount=amt,
            currency=_clean(d.get("currency"), upper=True),
            fund_name=_clean(d.get("fund_name")),
            recipient=_clean(d.get("recipient")),
            commitment_type=_clean(d.get("commitment_type"), lower=True),
            announced_at=_clean(d.get("announced_at")),
            rationale=_clean(d.get("rationale")),
        )


@dataclass
class BilateralMeeting:
    uae_principal: str | None
    foreign_principal: str | None
    foreign_country: str | None
    meeting_type: str | None        # call | meeting | visit | summit | bilateral
    when_iso: str | None
    location: str | None
    topics: list[str]

    @classmethod
    def from_dict(cls, d: dict) -> "BilateralMeeting":
        topics = d.get("topics") or []
        if not isinstance(topics, list):
            topics = [str(topics)]
        return cls(
            uae_principal=_clean(d.get("uae_principal")),
            foreign_principal=_clean(d.get("foreign_principal")),
            foreign_country=_clean(d.get("foreign_country"), upper=True),
            meeting_type=_clean(d.get("meeting_type"), lower=True),
            when_iso=_clean(d.get("when_iso")),
            location=_clean(d.get("location")),
            topics=[str(t).strip() for t in topics if str(t).strip()][:8],
        )


@dataclass
class Classification:
    category: str
    uae_relevance: float
    severity: float
    is_breaking_candidate: bool
    entities: list[dict]
    doctrine_relation: str | None
    one_line_summary: str
    rationale: str
    translated_title: str | None = None

    # New (migration 004) — per UAE Foreign Aid Policy 2026
    policy_sector: str | None = None              # one of POLICY_SECTORS
    country_focus: list[str] = field(default_factory=list)   # ISO-3166-1 alpha-2, max 3
    capital_signals: dict[str, bool] = field(default_factory=dict)
    graduation_signal: bool = False
    financial_commitments: list[FinancialCommitment] = field(default_factory=list)
    bilateral_meetings: list[BilateralMeeting] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "Classification":
        # Country focus: accept either a single string or a list. Normalise to
        # uppercase alpha-2 codes, drop anything that isn't 2 letters A-Z.
        cf_raw = d.get("country_focus") or []
        if isinstance(cf_raw, str):
            cf_raw = [cf_raw]
        country_focus = []
        for c in cf_raw[:5]:
            c2 = str(c).strip().upper()
            if len(c2) == 2 and c2.isalpha():
                country_focus.append(c2)
        # Cap at 3 — anything beyond signals the model wasn't being selective.
        country_focus = country_focus[:3]

        # Capital signals — only accept the canonical keys, coerce to bool.
        cs_raw = d.get("capital_signals") or {}
        if not isinstance(cs_raw, dict):
            cs_raw = {}
        capital_signals = {k: bool(cs_raw.get(k, False)) for k in CAPITAL_SIGNAL_KEYS}

        # Embedded commitments + meetings (may be missing on most items).
        fc_raw = d.get("financial_commitments") or []
        bm_raw = d.get("bilateral_meetings") or []
        fcs = [FinancialCommitment.from_dict(x) for x in fc_raw if isinstance(x, dict)][:5]
        bms = [BilateralMeeting.from_dict(x) for x in bm_raw if isinstance(x, dict)][:3]

        return cls(
            category=str(d.get("category", "other")),
            uae_relevance=float(d.get("uae_relevance", 0.0)),
            severity=float(d.get("severity", 0.0)),
            is_breaking_candidate=bool(d.get("is_breaking_candidate", False)),
            entities=list(d.get("entities", []) or []),
            doctrine_relation=d.get("doctrine_relation"),
            one_line_summary=str(d.get("one_line_summary", "")),
            rationale=str(d.get("rationale", "")),
            translated_title=_clean(d.get("translated_title")),
            policy_sector=_clean(d.get("policy_sector"), lower=True),
            country_focus=country_focus,
            capital_signals=capital_signals,
            graduation_signal=bool(d.get("graduation_signal", False)),
            financial_commitments=fcs,
            bilateral_meetings=bms,
        )


def _clean(v, *, lower: bool = False, upper: bool = False) -> str | None:
    """Return a stripped non-empty string, or None."""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    if lower:
        s = s.lower()
    elif upper:
        s = s.upper()
    return s[:500]


CATEGORIES = {
    "humanitarian",
    "aid_commitments",
    "reports_evidence",
    "conferences_events",
    "uae_foreign_policy_signals",
    "uae_leadership_doctrine",
    "uae_ecosystem_moves",
    "other",
}

DOCTRINE_RELATIONS = {"reinforcing", "refining", "evolving", "contradicting", "new", None}

# Allowed policy_sector values (from UAE Foreign Aid Policy 2026, Ch 4).
# Unknown/missing values are coerced to None — better than silently keeping
# a junk tag that pollutes the digest's sector views.
POLICY_SECTORS = {
    "water",
    "food",
    "energy",
    "natural-resources",
    "health",
    "education",
    "trade",
    "biz-enablement",
    "ai-tech",
    "soe",
    "humanitarian",
    "financing",
    "convening",
}

# Acronyms that should stay UPPERCASE even after title-casing the slug.
_TITLE_ACRONYMS = {
    "uae", "us", "usa", "uk", "un", "eu", "nato", "oecd", "imf",
    "who", "wfp", "fao", "iom", "unicef", "unhcr", "ohchr", "ocha",
    "icc", "icj", "g7", "g20", "g77", "asean", "ecowas", "ecb", "fed",
    "cia", "fbi", "nsa", "mi6", "mi5", "fco", "fcdo", "kgb", "isis",
    "uss", "uae's", "us's", "uk's", "ai", "vp", "ceo", "cfo", "pm",
    "mp", "mps", "hq", "tv", "bbc", "cnn", "ap", "afp", "wam",
    "adia", "adq", "adnoc", "mbz", "mbs", "mbr", "abu", "iaea",
    "icrc", "ngo", "ngos", "ip", "phd", "wto", "dpr", "drc",
    # UAE-ecosystem acronyms
    "gcc", "ihc", "ihpc", "oda", "erc", "ezp", "ihi", "rcrc",
    "sdg", "sdgs", "cop", "ldc", "ldcs", "oda's",
}

# Lowercase-stays-lowercase (mid-sentence connectors). First word still gets capitalised regardless.
_TITLE_LOWER = {
    "a", "an", "and", "as", "at", "but", "by", "for", "from", "in",
    "of", "on", "or", "the", "to", "vs", "via", "with",
}

def title_case_clean(title: str) -> str:
    s = (title or "").strip()
    if not s:
        return s
    
    # Strip trailing numeric or decimal numbers to fix weird ID endings
    import re
    s = re.sub(r'\s+[\d\.]+$', '', s).strip()
    s = re.sub(r'\s+[a-f0-9]{6,32}$', '', s, flags=re.IGNORECASE).strip()
    
    words = s.split()
    if not words: return ""
    last_i = len(words) - 1
    out: list[str] = []
    for i, word in enumerate(words):
        lw = word.lower()
        if lw in _TITLE_ACRONYMS:
            out.append(lw.upper())
        elif 0 < i < last_i and lw in _TITLE_LOWER:
            out.append(lw)
        elif "'" in word:
            head, _, tail = word.partition("'")
            out.append(head.capitalize() + "'" + tail.lower())
        else:
            out.append(word.capitalize())
    return " ".join(out)
