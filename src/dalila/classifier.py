"""Classifier — Haiku 4.5 via the `claude` CLI.

The system prompt is the classifier instructions + the full entity watchlist.
Sending it on every call lets Claude Code's internal cache hit on subsequent
calls within the cache TTL window.

Items are classified in batches (default 10 per call) to amortise CLI
process-spawn overhead (~2-4s/call on Windows) and reuse the cached system
prompt across many items in a single invocation.
"""

from __future__ import annotations

import json
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
    """Classify a single news item. Thin wrapper over classify_batch([item])."""
    [c] = classify_batch([{"title": title, "body": body, "url": url}])
    return c


def classify_batch(items: list[dict]) -> list[Classification]:
    """Classify N items in ONE LLM call. Returns N Classifications in input order.

    Each item is a dict with `title`, `body`, and optional `url`.

    The model is asked to return JSON of shape `{"items": [{"n": 1, ...}, ...]}`
    where `n` matches the 1-based position of each input item. We sort the
    response by `n` to recover input ordering, then validate/normalise each
    Classification just like the single-item path used to.

    Raises LLMError on transient failure (rate limit, network, timeout) or
    ValueError if the response shape is invalid after one retry.
    """
    if not items:
        return []

    user_msg = _build_batch_user_message(items)
    data = llm.call_json(
        model=llm.HAIKU,
        system_prompt=_classifier_system_prompt(),
        user_prompt=user_msg,
        purpose="classifier",
        timeout=240,    # batch can take longer than single item
    )

    arr = _extract_items_array(data, expected=len(items))
    return [_normalise(Classification.from_dict(d)) for d in arr]


def _build_batch_user_message(items: list[dict]) -> str:
    """Compose a single user message containing N items to classify."""
    n = len(items)
    lines = [
        f"Classify each of the {n} item(s) below. Return JSON of EXACTLY this shape:",
        "",
        '{"items": [',
        '  {"n": 1, "category": "...", "uae_relevance": 0.0, "severity": 0.0, '
        '"is_breaking_candidate": false, "entities": [...], "doctrine_relation": null, '
        '"one_line_summary": "...", "rationale": "..."},',
        '  {"n": 2, ...},',
        "  ...",
        "]}",
        "",
        "The `n` field must match the item number below (1-based). Every n from "
        f"1 to {n} must appear EXACTLY once. The order in the response array does "
        "not need to match input order — we sort by `n`. Each item's fields must "
        "follow the system-prompt schema. Return ONLY the JSON object — no prose, "
        "no markdown fences. First character of your response must be `{`.",
        "",
        "===== ITEMS =====",
    ]
    for idx, item in enumerate(items, start=1):
        title = item.get("title") or ""
        url = item.get("url")
        body = (item.get("body") or "").strip()
        if len(body) > 3000:
            body = body[:3000] + " […truncated]"
        lines.append(f"\n#{idx}")
        lines.append(f"Title: {title}")
        if url:
            lines.append(f"URL: {url}")
        if body:
            lines.append(f"Body: {body}")
    return "\n".join(lines)


def _extract_items_array(data, expected: int) -> list[dict]:
    """Pull and sanity-check the items array from the model response."""
    # Tolerate both `{"items": [...]}` and a bare top-level array (in case the
    # model decides to skip the wrapper).
    if isinstance(data, list):
        arr = data
    elif isinstance(data, dict) and isinstance(data.get("items"), list):
        arr = data["items"]
    else:
        raise ValueError(f"batch response missing items array; got keys={list(data.keys()) if isinstance(data, dict) else type(data).__name__}")

    if len(arr) != expected:
        raise ValueError(f"batch response had {len(arr)} items, expected {expected}")

    # Sort by `n` so positions line up with input. Items without `n` go to end.
    arr = sorted(arr, key=lambda d: d.get("n") if isinstance(d.get("n"), int) else 10**9)
    return arr


def _normalise(c: Classification) -> Classification:
    """Clamp floats and validate category — same logic as the old per-item path."""
    if c.category not in CATEGORIES:
        log.warning("classifier returned unknown category %r; coercing to 'other'", c.category)
        c.category = "other"
    c.uae_relevance = max(0.0, min(1.0, c.uae_relevance))
    c.severity = max(0.0, min(1.0, c.severity))
    if c.doctrine_relation in ("", "null"):
        c.doctrine_relation = None
    return c
