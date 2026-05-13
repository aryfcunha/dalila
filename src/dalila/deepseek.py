"""DeepSeek classifier backend — OpenAI-compatible HTTP API.

This is a SECONDARY backend used for backfill jobs only. The primary classifier
remains `claude` CLI → Haiku 4.5 (see `classifier.py` and the invariant in
CLAUDE.md). DeepSeek's `deepseek-chat` is roughly 1/20th the cost of Haiku via
API and bypasses the shared Claude Code subscription rate limit pool, which
makes it the right tool for one-shot historical backfills (Jan 1 → today)
without burning the user's daily Claude quota.

Design choices:
  * Same system prompt + user message builder as `classifier.py` — every
    field downstream depends on the prompt's contract (categories, ISO-2
    country codes, policy_sector enum). Diverging the prompt would silently
    change the schema. We import them, not duplicate them.
  * stdlib `urllib.request` — no new dependency. The codebase already uses
    `httpx` for Telegram, but pulling in another transport here is overkill
    for one POST endpoint.
  * API key sourced from env (`DEEPSEEK_API_KEY`). NEVER hard-coded.
  * Same error semantics as the Claude path — raise `LLMError` (imported
    from `dalila.llm`) on transient failures so `pipeline.run_classify`'s
    existing rate-limit-handling branch catches them.

Caveats:
  * No prompt caching equivalent. DeepSeek does have prompt caching but it's
    automatic and works per-prefix; the system prompt being identical across
    calls already gets us their cache discount. No tuning needed.
  * `response_format={"type": "json_object"}` is supported by DeepSeek and
    we use it to guarantee parseable output.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request

from dalila.classifier import (
    _build_batch_user_message,
    _classifier_system_prompt,
    _extract_items_array,
    _normalise,
)
from dalila.db import connect, record_llm_call
from dalila.llm import LLMError
from dalila.models import Classification

log = logging.getLogger(__name__)

DEEPSEEK_CHAT = "deepseek-chat"
DEEPSEEK_REASONER = "deepseek-reasoner"

_API_URL = "https://api.deepseek.com/chat/completions"


def classify_batch(items: list[dict], *, model: str = DEEPSEEK_CHAT) -> list[Classification]:
    """Classify N items in ONE DeepSeek API call. Signature-compatible with
    `classifier.classify_batch` so it's a drop-in replacement.

    Raises LLMError on transient HTTP / network failure or unparseable response.
    """
    if not items:
        return []

    user_msg = _build_batch_user_message(items)
    raw = _call_chat(
        model=model,
        system_prompt=_classifier_system_prompt(),
        user_prompt=user_msg,
        purpose="classifier",
        timeout=240,
    )

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        # Retry once with corrective prompt — same recovery pattern as llm.call_json
        log.warning("deepseek: first JSON parse failed (%s); retrying", exc)
        corrective = (
            f"\n\nYour previous response was not valid JSON ({exc}). "
            "Re-emit as a single valid JSON object only. First character must be `{`."
        )
        raw = _call_chat(
            model=model,
            system_prompt=_classifier_system_prompt(),
            user_prompt=user_msg + corrective,
            purpose="classifier",
            timeout=240,
        )
        data = json.loads(raw)

    arr = _extract_items_array(data, expected=len(items))
    return [_normalise(Classification.from_dict(d)) for d in arr]


def _call_chat(
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    purpose: str,
    timeout: int = 120,
) -> str:
    """POST to DeepSeek's chat completions endpoint. Returns the content string.

    Logs to llm_call_log like the Claude path so cost/throughput introspection
    (`dalila status`) sees these calls too.
    """
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise LLMError(
            "DEEPSEEK_API_KEY not set. Add it to .env or export it before running "
            "classification with --backend deepseek."
        )

    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.0,
        "max_tokens": 8000,
    }).encode("utf-8")

    req = urllib.request.Request(
        _API_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )

    start = time.monotonic()
    error: str | None = None
    success = False
    text = ""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        payload = json.loads(raw)
        choices = payload.get("choices") or []
        if not choices:
            raise LLMError(f"deepseek: no choices in response: {raw[:300]}")
        text = (choices[0].get("message") or {}).get("content") or ""
        if not text.strip():
            raise LLMError("deepseek: empty content in response")
        success = True
    except urllib.error.HTTPError as exc:
        body_text = ""
        try:
            body_text = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        error = f"HTTP {exc.code}: {body_text or exc.reason}"
        # Map 429 / 5xx to rate-limit phrasing so pipeline._is_transient_error catches it
        if exc.code == 429 or exc.code >= 500:
            raise LLMError(f"rate limit / transient: {error}") from None
        raise LLMError(f"deepseek API error: {error}") from None
    except urllib.error.URLError as exc:
        error = f"network: {exc.reason}"
        raise LLMError(f"deepseek network error: {error}") from None
    except json.JSONDecodeError as exc:
        error = f"non-JSON response: {exc}"
        raise LLMError(error) from None
    finally:
        duration_ms = int((time.monotonic() - start) * 1000)
        try:
            with connect() as conn:
                record_llm_call(
                    conn, model=model, purpose=purpose,
                    duration_ms=duration_ms, success=success, error=error,
                )
        except Exception as log_exc:
            log.warning("failed to record llm call: %s", log_exc)

    return text
