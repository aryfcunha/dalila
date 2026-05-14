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
        result = subprocess.run([bin_path, "--version"], capture_output=True, text=True, timeout=10)
        return (True, f"claude CLI ok at {bin_path}") if result.returncode == 0 else (False, "claude CLI failed")
    except Exception as exc:
        return False, str(exc)

def _run_claude(
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    purpose: str,
    timeout: int = 120,
) -> LLMResponse:
    cfg = get_config()
    bin_path = _resolve_claude_bin() or cfg.claude_bin
    
    start = time.monotonic()
    success = False
    error = None
    text = ""

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

    except (FileNotFoundError, LLMError) as exc:
        # Fallback to DeepSeek if CLI is missing or failed (and we have a key)
        ds_key = os.getenv("DEEPSEEK_API_KEY")
        if ds_key:
            log.info("Falling back to DeepSeek for %s", purpose)
            resp = _call_deepseek(model, system_prompt, user_prompt, purpose, timeout)
            success = True
            return resp
        raise LLMError(f"Claude CLI failed and no DeepSeek fallback available: {exc}")
    except Exception as exc:
        error = str(exc)
        raise
    finally:
        duration_ms = int((time.monotonic() - start) * 1000)
        try:
            with connect() as conn:
                record_llm_call(conn, model=model, purpose=purpose, duration_ms=duration_ms, success=success, error=error)
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
