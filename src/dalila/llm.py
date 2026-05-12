"""LLM backend: subprocess wrapper around the `claude` CLI.

This is the entire surface area for talking to Claude. Both the classifier
(Haiku 4.5) and the editor (Sonnet 4.6) go through here. The wrapper:

  * Spawns `claude -p ... --append-system-prompt ... --model ...`
  * Captures stdout
  * Logs the call (model, duration, success) to llm_call_log
  * Has a JSON helper (call_json) that retries once on a parse failure

CLI flags assumed (verify against `claude --help` on your version — these are
the documented stable flags as of Claude Code 2.x):

    -p, --print                  Non-interactive one-shot mode
    --model <id>                 e.g. "claude-haiku-4-5" or "haiku"
    --append-system-prompt <s>   Adds to system prompt
    --max-turns <n>              Cap turn count (we use 1 — no tool use loop)

If your `claude` version differs, edit the args list in `_run_claude` below;
nothing else in the codebase touches the CLI directly.
"""

from __future__ import annotations

import json
import logging
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
    """Resolve the configured CLI name to an absolute executable path.

    On Windows, `claude` is shipped as `claude.cmd` (a node-bin shim); plain
    subprocess.run won't find .cmd / .bat files unless we pass the full path.
    `shutil.which` finds them when PATHEXT includes .CMD (it does by default).
    """
    cfg = get_config()
    return shutil.which(cfg.claude_bin)


def check_cli_available() -> tuple[bool, str]:
    """Return (ok, message). Run this at startup before scheduling anything."""
    cfg = get_config()
    bin_path = _resolve_claude_bin()
    if not bin_path:
        return False, f"`{cfg.claude_bin}` not found on PATH. Install Claude Code and run `claude login` first."
    try:
        result = subprocess.run(
            [bin_path, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return False, f"`{bin_path} --version` failed: {result.stderr.strip()}"
        return True, f"claude CLI ok at {bin_path} ({result.stdout.strip()})"
    except subprocess.TimeoutExpired:
        return False, f"`{bin_path} --version` timed out"
    except Exception as exc:
        return False, f"`{bin_path} --version` raised: {exc}"


def _run_claude(
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    purpose: str,
    timeout: int = 120,
) -> LLMResponse:
    """Low-level call. Logs to llm_call_log on every invocation.

    The system prompt + user prompt are combined and piped via stdin. Passing
    a 25KB system prompt as an argv element exceeds Windows' command-line limit
    (~8KB for CMD, 32KB for CreateProcess but unreliable). Stdin sidesteps this.
    The combined prompt opens with the system instructions and ends with the
    item-to-classify, separated by a clear delimiter so the model treats the
    leading section as instructions.
    """
    cfg = get_config()
    bin_path = _resolve_claude_bin() or cfg.claude_bin

    # We don't use --system-prompt / --append-system-prompt because the watchlist
    # is too long for argv. Instead we frame the combined prompt explicitly.
    combined_prompt = (
        "You are operating under the following system instructions. Follow them precisely.\n\n"
        "===== SYSTEM INSTRUCTIONS =====\n"
        + system_prompt.strip()
        + "\n===== END SYSTEM INSTRUCTIONS =====\n\n"
        "===== USER INPUT =====\n"
        + user_prompt.strip()
        + "\n===== END USER INPUT =====\n"
    )

    args = [
        bin_path,
        "-p",                       # print mode (non-interactive)
        "--model", model,
        "--no-session-persistence", # no resume state for a one-shot
    ]

    start = time.monotonic()
    error: str | None = None
    success = False
    text = ""
    try:
        result = subprocess.run(
            args,
            input=combined_prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            # claude sometimes writes user-facing errors to stdout (e.g. rate limits)
            stderr_msg = (result.stderr or "").strip()
            stdout_msg = (result.stdout or "").strip()
            error = (stderr_msg or stdout_msg or f"exit {result.returncode}")[:500]
            raise LLMError(f"claude CLI exit {result.returncode}: {error}")
        text = (result.stdout or "").strip()
        if not text:
            error = "empty stdout"
            raise LLMError("claude CLI returned empty stdout")
        # Detect rate-limit text in a successful-exit response (rare; CLI usually
        # exits non-zero on limit, but defensively check).
        lower = text.lower()
        if "you've hit your limit" in lower or "rate limit" in lower and len(text) < 200:
            error = text[:200]
            raise LLMError(f"rate limited: {error}")
        success = True
    except subprocess.TimeoutExpired:
        error = f"timeout after {timeout}s"
        raise LLMError(error) from None
    except FileNotFoundError:
        error = f"`{cfg.claude_bin}` not on PATH"
        raise LLMError(error) from None
    finally:
        duration_ms = int((time.monotonic() - start) * 1000)
        try:
            with connect() as conn:
                record_llm_call(conn, model=model, purpose=purpose, duration_ms=duration_ms, success=success, error=error)
        except Exception as log_exc:
            # Logging failure should not mask the real error.
            log.warning("failed to record llm call: %s", log_exc)

    return LLMResponse(text=text, duration_ms=duration_ms)


def call(*, model: str, system_prompt: str, user_prompt: str, purpose: str, timeout: int = 120) -> str:
    """Plain-text call. Use for the editor."""
    return _run_claude(
        model=model,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        purpose=purpose,
        timeout=timeout,
    ).text


def call_json(*, model: str, system_prompt: str, user_prompt: str, purpose: str, timeout: int = 120) -> dict:
    """JSON-output call. Strips markdown fences, retries once on parse failure.

    The classifier prompt instructs the model to return raw JSON, but in practice
    models occasionally wrap output in ```json fences. We tolerate that.
    """
    raw = call(model=model, system_prompt=system_prompt, user_prompt=user_prompt, purpose=purpose, timeout=timeout)
    try:
        return _parse_json_lenient(raw)
    except ValueError as first_err:
        log.warning("first JSON parse failed (%s); retrying with corrective prompt", first_err)
        corrective = (
            "Your previous response was not valid JSON.\n"
            f"Parser error: {first_err}\n"
            "Re-emit the same classification as a single valid JSON object only. "
            "No prose, no markdown fences. The first character must be `{`."
        )
        retry_raw = call(
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt + "\n\n" + corrective,
            purpose=purpose,
            timeout=timeout,
        )
        return _parse_json_lenient(retry_raw)


def _parse_json_lenient(text: str):
    """Parse JSON; tolerate ```json fences and leading/trailing prose.

    Returns whatever shape json.loads produces — caller decides if it expects
    a dict, a list, or either. (Batch classifier accepts both shapes.)
    """
    s = text.strip()
    if s.startswith("```"):
        # Strip first fence line and any closing fence.
        lines = s.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines).strip()

    # If still not pure JSON, try to extract the {...} or [...] span.
    if not s.startswith(("{", "[")):
        obj_start = s.find("{")
        arr_start = s.find("[")
        starts = [p for p in (obj_start, arr_start) if p != -1]
        if not starts:
            raise ValueError("no JSON object or array found in response")
        start = min(starts)
        # Find the matching close — pick the last } or ] in the string
        end = max(s.rfind("}"), s.rfind("]"))
        if end <= start:
            raise ValueError("could not locate JSON span in response")
        s = s[start : end + 1]

    return json.loads(s)


# Convenience aliases — keep model IDs in one place
HAIKU = "claude-haiku-4-5"
SONNET = "claude-sonnet-4-6"
