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

    @classmethod
    def from_dict(cls, d: dict) -> "Classification":
        return cls(
            category=str(d.get("category", "other")),
            uae_relevance=float(d.get("uae_relevance", 0.0)),
            severity=float(d.get("severity", 0.0)),
            is_breaking_candidate=bool(d.get("is_breaking_candidate", False)),
            entities=list(d.get("entities", []) or []),
            doctrine_relation=d.get("doctrine_relation"),
            one_line_summary=str(d.get("one_line_summary", "")),
            rationale=str(d.get("rationale", "")),
        )


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
