"""LLM backend: subprocess wrapper around the `claude` CLI with DeepSeek fallback.

Three DeepSeek paths exist:
  • Manual override — DALILA_LLM_BACKEND=deepseek routes every call to DeepSeek.
  • Automatic fallback — on a Claude capacity error (quota / rate-limit / overload)
    the call transparently retries on DeepSeek, with a shared cooldown so we stop
    re-spawning a throttled CLI. Toggle with DALILA_DEEPSEEK_FALLBACK (default ON
    when DEEPSEEK_API_KEY is set). This keeps the daily brief shipping when the
    Claude subscription is exhausted.
  • Missing-binary fallback — if the `claude` CLI isn't found at all.
"""

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

# Capacity-class failures from the `claude` CLI that mean "Claude can't serve
# this right now" — quota exhaustion, rate limits, transient overload. On any
# of these we route to DeepSeek (when DALILA_DEEPSEEK_FALLBACK is enabled) so
# the daily brief still ships, instead of the scheduler simply backing off and
# producing nothing. Auth / bad-request / prompt errors are deliberately
# EXCLUDED: DeepSeek can't fix those and they must stay visible (and the
# scheduler's rate-limit back-off still engages when the fallback is off).
_FALLBACK_SIGNALS = (
    "you've hit your limit", "usage limit", "rate limit", "rate_limit",
    "ratelimit", "429", "quota", "insufficient", "overloaded",
    "capacity", "503", "529",
)

# Monotonic-clock deadline. While now < this, skip Claude and go straight to
# DeepSeek — set after a capacity error so we don't pay a doomed CLI spawn on
# every call during a multi-hour quota window. Module-level so the cooldown is
# shared across all call sites (they draw on one quota pool).
_claude_cooldown_until: float = 0.0


def _auto_fallback_enabled() -> bool:
    """Automatic DeepSeek fallback on Claude capacity errors. Defaults ON when
    a DeepSeek key is present; force off with DALILA_DEEPSEEK_FALLBACK=0."""
    if not os.getenv("DEEPSEEK_API_KEY"):
        return False
    return os.getenv("DALILA_DEEPSEEK_FALLBACK", "1").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _is_capacity_error(err_msg: str) -> bool:
    low = err_msg.lower()
    return any(sig in low for sig in _FALLBACK_SIGNALS)


def _fallback_cooldown_seconds() -> int:
    try:
        return max(0, int(os.getenv("DALILA_DEEPSEEK_FALLBACK_COOLDOWN_MINUTES", "30"))) * 60
    except ValueError:
        return 30 * 60


def _run_claude(
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    purpose: str,
    timeout: int = 120,
) -> LLMResponse:
    # Backend override: route the live LLM path through DeepSeek when the
    # Claude Code subscription is unavailable (org-disabled) or otherwise not
    # wanted. Enable with DALILA_LLM_BACKEND=deepseek in the environment/.env.
    if os.getenv("DALILA_LLM_BACKEND", "").strip().lower() == "deepseek" and os.getenv("DEEPSEEK_API_KEY"):
        _start = time.monotonic()
        _ok, _err = False, None
        try:
            _resp = _call_deepseek(model, system_prompt, user_prompt, purpose, timeout)
            _ok = True
            return _resp
        except Exception as _exc:
            _err = str(_exc)
            raise
        finally:
            try:
                with connect() as _conn:
                    record_llm_call(_conn, model=model, purpose=purpose,
                                    duration_ms=int((time.monotonic() - _start) * 1000),
                                    success=_ok, error=_err)
            except Exception:
                pass

    global _claude_cooldown_until
    cfg = get_config()
    bin_path = _resolve_claude_bin() or cfg.claude_bin

    start = time.monotonic()
    success = False
    error = None
    used_model = model  # switches to the DeepSeek model if we fall back

    def _fallback(reason: str) -> LLMResponse:
        nonlocal used_model, success
        log.warning("Claude unavailable for %s (%s) — falling back to DeepSeek", purpose, reason)
        resp = _call_deepseek(model, system_prompt, user_prompt, purpose, timeout)
        used_model = "deepseek-chat"
        success = True
        return resp

    try:
        # Still inside a cooldown from a recent capacity error → don't even
        # spawn the CLI; Claude is almost certainly still throttled.
        if _auto_fallback_enabled() and time.monotonic() < _claude_cooldown_until:
            return _fallback("in Claude cooldown window")

        combined_prompt = (
            "You are operating under the following system instructions.\n\n"
            "===== SYSTEM INSTRUCTIONS =====\n" + system_prompt.strip() + "\n===== END SYSTEM INSTRUCTIONS =====\n\n"
            "===== USER INPUT =====\n" + user_prompt.strip() + "\n===== END USER INPUT =====\n"
        )

        args = [bin_path, "-p", "--model", model, "--no-session-persistence"]

        try:
            result = subprocess.run(
                args, input=combined_prompt, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout, check=False
            )
        except FileNotFoundError as exc:
            # Claude CLI binary is missing — fall back to DeepSeek if a key is
            # configured (any backend beats no brief).
            if os.getenv("DEEPSEEK_API_KEY"):
                return _fallback(f"CLI not found: {exc}")
            raise LLMError(f"Claude CLI not found and no DeepSeek fallback available: {exc}")

        if result.returncode != 0:
            stderr_msg = (result.stderr or "").strip()
            stdout_msg = (result.stdout or "").strip()
            err = (stderr_msg or stdout_msg or f"exit {result.returncode}")[:500]
            # Quota / rate-limit / overload → switch to DeepSeek instead of
            # failing, and start a cooldown so we stop probing a throttled
            # Claude every tick. Other non-zero exits (auth, bad request)
            # propagate as LLMError so they stay visible and the scheduler's
            # rate-limit back-off still engages when the fallback is disabled.
            if _auto_fallback_enabled() and _is_capacity_error(err):
                _claude_cooldown_until = time.monotonic() + _fallback_cooldown_seconds()
                return _fallback(err[:160])
            error = err
            raise LLMError(f"claude CLI exit {result.returncode}: {err}")

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
        duration_ms = int((time.monotonic() - start) * 1000)
        try:
            with connect() as conn:
                record_llm_call(conn, model=used_model, purpose=purpose, duration_ms=duration_ms, success=success, error=error)
        except Exception:
            pass

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
