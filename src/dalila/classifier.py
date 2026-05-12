"""Classifier — Haiku 4.5 via the `claude` CLI.

The system prompt is the classifier instructions + the full entity watchlist.
Sending it on every call means Claude Code's internal caching kicks in for the
shared prefix (we can't verify cache hits without the API's usage object, but
the cache is deterministic over identical text).
"""

from __future__ import annotations

import logging
from functools import lru_cache

from dalila import llm
from dalila.config import load_entities_yaml_text, load_prompt
from dalila.models import CATEGORIES, Classification

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _classifier_system_prompt() -> str:
    instructions = load_prompt("classifier")
    entities = load_entities_yaml_text()
    return (
        instructions
        + "\n\n---\n\n## Entity watchlist\n\n"
        "The following YAML is the canonical entity watchlist. Match item content "
        "against `name` and `aliases` when tagging entities. Use this to anchor "
        "UAE-relevance scoring.\n\n"
        "```yaml\n" + entities + "\n```\n"
    )


def classify(title: str, body: str | None, url: str | None = None) -> Classification:
    """Classify one news item; returns a Classification.

    Raises LLMError on CLI failure, ValueError on persistent JSON parse failure.
    """
    user_msg = _build_user_message(title=title, body=body, url=url)
    data = llm.call_json(
        model=llm.HAIKU,
        system_prompt=_classifier_system_prompt(),
        user_prompt=user_msg,
        purpose="classifier",
        timeout=60,
    )
    c = Classification.from_dict(data)
    # Defensive normalisation — clamp floats, validate category
    if c.category not in CATEGORIES:
        log.warning("classifier returned unknown category %r; coercing to 'other'", c.category)
        c.category = "other"
    c.uae_relevance = max(0.0, min(1.0, c.uae_relevance))
    c.severity = max(0.0, min(1.0, c.severity))
    if c.doctrine_relation in ("", "null"):
        c.doctrine_relation = None
    return c


def _build_user_message(*, title: str, body: str | None, url: str | None) -> str:
    parts = [f"Title: {title}"]
    if url:
        parts.append(f"URL: {url}")
    if body:
        # Keep body bounded — full articles can be huge.
        truncated = body.strip()
        if len(truncated) > 4000:
            truncated = truncated[:4000] + " […truncated]"
        parts.append(f"Body: {truncated}")
    parts.append("\nClassify the above item per the system prompt. Return ONLY the JSON object.")
    return "\n\n".join(parts)
