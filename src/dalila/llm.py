"""LLM backend: subprocess wrapper around the `claude` CLI with DeepSeek fallback."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from functools import lru_cache

from dalila.config import get_config
from dalila.db import connect, record_llm_call

log = logging.getLogger(__name__)

class LLMError(RuntimeError):
    """Raised when the `claude` CLI fails or returns unusable output."""

@dataclass
class LLMResponse:
    text: str
    duration_ms: int

@lru_cache(maxsize=1)
def _resolve_claude_bin() -> str | None:
    cfg = get_config()
    return shutil.which(cfg.claude_bin)

def check_cli_available() -> tuple[bool, str]:
    cfg = get_config()
    bin_path = _resolve_claude_bin()
    if not bin_path:
        return False, f"`{cfg.claude_bin}` not found on PATH."
    try:
        # 30s, not 10: on a resource-starved VM (low RAM + swap thrash) a cold
        # node start for `claude --version` can legitimately take >10s. A tight
        # timeout here previously made the bot's startup probe fail and, under
        # systemd Restart=always, crash-loop the service.
        result = subprocess.run([bin_path, "--version"], capture_output=True, text=True, timeout=30)
        return (True, f"claude CLI ok at {bin_path}") if result.returncode == 0 else (False, "claude CLI failed")
    except Exception as exc:
        return False, str(exc)

def _looks_like_rate_limit(msg: str) -> bool:
    """True if a CLI error is a Claude usage/rate-limit — a FREE, self-healing
    condition. These must NOT trigger the paid DeepSeek fallback: the scheduler
    pauses classify until the window resets and retries on Haiku at $0. Network
    blips, auth failures and hard errors are deliberately excluded here so they
    DO fall back to DeepSeek (the 'if that doesn't work' path)."""
    m = (msg or "").lower()
    return any(t in m for t in (
        # Substrings, contraction-agnostic ("you've"/"you have" both match).
        "hit your limit", "reached your limit", "limit reached",
        "rate limit", "rate_limit", "usage limit", "usage_limit",
        "429", "quota",
    ))


def _deepseek_available() -> bool:
    return bool(os.getenv("DEEPSEEK_API_KEY"))


def _record(model: str, purpose: str, start: float, success: bool, error: str | None) -> None:
    """Best-effort persist of an LLM call for cost/health accounting."""
    try:
        with connect() as conn:
            record_llm_call(conn, model=model, purpose=purpose,
                            duration_ms=int((time.monotonic() - start) * 1000),
                            success=success, error=error)
    except Exception:
        pass


def _deepseek_recorded(model: str, system_prompt: str, user_prompt: str,
                       purpose: str, timeout: int) -> "LLMResponse":
    start = time.monotonic()
    ok, err = False, None
    try:
        resp = _call_deepseek(model, system_prompt, user_prompt, purpose, timeout)
        ok = True
        return resp
    except Exception as exc:
        err = str(exc)
        raise
    finally:
        # Record under a deepseek-tagged model name so cost accounting can tell
        # the paid fallback apart from the free Haiku path.
        _record(f"deepseek:{model}", purpose, start, ok, err)


def _run_claude_cli(*, model: str, system_prompt: str, user_prompt: str,
                    purpose: str, timeout: int) -> "LLMResponse":
    """One call via the `claude` CLI (Haiku, Max-plan, $0 marginal).

    Records the attempt and raises FileNotFoundError (binary missing) or
    LLMError (non-zero exit / empty output / rate-limit) on failure so the
    caller can decide whether to fall back."""
    cfg = get_config()
    bin_path = _resolve_claude_bin() or cfg.claude_bin
    start = time.monotonic()
    success = False
    error = None
    try:
        combined_prompt = (
            "You are operating under the following system instructions.\n\n"
            "===== SYSTEM INSTRUCTIONS =====\n" + system_prompt.strip() + "\n===== END SYSTEM INSTRUCTIONS =====\n\n"
            "===== USER INPUT =====\n" + user_prompt.strip() + "\n===== END USER INPUT =====\n"
        )
        args = [bin_path, "-p", "--model", model, "--no-session-persistence"]
        result = subprocess.run(
            args, input=combined_prompt, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, check=False
        )
        if result.returncode != 0:
            stderr_msg = (result.stderr or "").strip()
            stdout_msg = (result.stdout or "").strip()
            error = (stderr_msg or stdout_msg or f"exit {result.returncode}")[:500]
            raise LLMError(f"claude CLI exit {result.returncode}: {error}")

        text = (result.stdout or "").strip()
        if not text:
            raise LLMError("claude CLI returned empty stdout")

        success = True
        return LLMResponse(text=text, duration_ms=int((time.monotonic() - start) * 1000))
    except Exception as exc:
        if error is None:
            error = str(exc)
        raise
    finally:
        _record(model, purpose, start, success, error)


def _run_claude(
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    purpose: str,
    timeout: int = 120,
) -> LLMResponse:
    # Explicit manual override: force DeepSeek for the WHOLE live path. This is
    # opt-in and intended only for cost-bounded one-shot backfill runs (set
    # DALILA_LLM_BACKEND=deepseek). It is NOT the live default — normal
    # operation runs Haiku via the CLI below. Leaving this set on the VM is what
    # silently routed every live call through the paid API.
    if os.getenv("DALILA_LLM_BACKEND", "").strip().lower() == "deepseek" and _deepseek_available():
        log.info("DALILA_LLM_BACKEND=deepseek — forcing DeepSeek for %s (manual override)", purpose)
        return _deepseek_recorded(model, system_prompt, user_prompt, purpose, timeout)

    # ---- Primary: Haiku via the `claude` CLI ($0 marginal on the Max plan) ---
    try:
        return _run_claude_cli(model=model, system_prompt=system_prompt,
                               user_prompt=user_prompt, purpose=purpose, timeout=timeout)
    except (FileNotFoundError, LLMError) as exc:
        msg = str(exc)
        # A usage/rate-limit is free and self-healing — never spill to the paid
        # DeepSeek path. Re-raise so the scheduler's back-off logic parses the
        # reset time and retries on Haiku when the window opens.
        if isinstance(exc, LLMError) and _looks_like_rate_limit(msg):
            raise
        # Any other CLI failure (binary missing, auth broken, timeout, malformed
        # output) is a genuine "the CLI can't do this right now" — fall back to
        # DeepSeek if a key is configured, so classification still happens.
        if _deepseek_available():
            log.warning("claude CLI unusable for %s (%s) — falling back to DeepSeek",
                        purpose, msg[:200])
            return _deepseek_recorded(model, system_prompt, user_prompt, purpose, timeout)
        raise


def _call_deepseek(model: str, system_prompt: str, user_prompt: str, purpose: str, timeout: int) -> LLMResponse:
    """Fallback using DeepSeek API with urllib (proven path on VM)."""
    import urllib.request
    import urllib.error
    
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise LLMError("DeepSeek fallback failed: DEEPSEEK_API_KEY not found in environment")
    
    url = "https://api.deepseek.com/v1/chat/completions"
    
    # Use standard roles + headers from working deepseek.py module
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        "temperature": 0.0,
        "max_tokens": 8000,
    }
    body = json.dumps(payload).encode("utf-8")
    
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Dalila/1.0",
        },
    )
    
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            text = data["choices"][0]["message"]["content"]
            return LLMResponse(text=text, duration_ms=int((time.monotonic() - start) * 1000))
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8")
        log.warning("DeepSeek API failed (%d): %s", exc.code, err_body)
        raise LLMError(f"DeepSeek API error {exc.code}: {err_body}") from None
    except Exception as exc:
        log.warning("DeepSeek fallback exception: %s", exc)
        raise LLMError(f"DeepSeek fallback exception: {exc}") from None

def call(*, model: str, system_prompt: str, user_prompt: str, purpose: str, timeout: int = 120) -> str:
    return _run_claude(model=model, system_prompt=system_prompt, user_prompt=user_prompt, purpose=purpose, timeout=timeout).text

def call_json(*, model: str, system_prompt: str, user_prompt: str, purpose: str, timeout: int = 120) -> dict:
    raw = call(model=model, system_prompt=system_prompt, user_prompt=user_prompt, purpose=purpose, timeout=timeout)
    return _parse_json_lenient(raw)

def _parse_json_lenient(text: str):
    s = text.strip()
    if s.startswith("```"):
        lines = s.splitlines()
        if lines and lines[0].startswith("```"): lines = lines[1:]
        if lines and lines[-1].startswith("```"): lines = lines[:-1]
        s = "\n".join(lines).strip()
    if not s.startswith(("{", "[")):
        obj_start, arr_start = s.find("{"), s.find("[")
        starts = [p for p in (obj_start, arr_start) if p != -1]
        if not starts: raise ValueError("no JSON found")
        start = min(starts)
        end = max(s.rfind("}"), s.rfind("]"))
        s = s[start : end + 1]
    return json.loads(s)

HAIKU = "claude-haiku-4-5"
SONNET = "claude-sonnet-4-6"
