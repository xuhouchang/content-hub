#!/usr/bin/env python3
"""LLM calls for the content pipeline, routed directly to the DeepSeek API."""

import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Optional

# ── Ensure .env is loaded before reading env vars ──
_dotenv_path = Path(__file__).resolve().parent.parent / ".env"
if _dotenv_path.exists():
    with open(_dotenv_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            if value and not os.environ.get(key):
                os.environ[key] = value

# Retained for explicit legacy call_openrouter() imports. call_model() never
# uses this provider after the DeepSeek-only routing change.
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "") or os.environ.get("LLM_API_KEY", "")
OPENROUTER_BASE = "https://openrouter.ai/api/v1"

# ── DeepSeek Direct Config ──
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "") or os.environ.get("LLM_API_KEY", "")
DEEPSEEK_BASE = "https://api.deepseek.com"
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash").strip()
DEEPSEEK_MODELS = {"deepseek-flash", "deepseek-v4-pro"}
MODEL_ALIASES = {
    "deepseek-v4-flash": "deepseek-flash",
    "deepseek/deepseek-v4-flash": "deepseek-flash",
}

OPENROUTER_MODEL_ALIASES = {
    "deepseek-v4-flash": "deepseek/deepseek-v4-flash",
    "openai-codex/gpt-5.4": "openai/gpt-5.4",
}

# ── Default Writing Model ──
DEFAULT_WRITING_MODEL = DEEPSEEK_MODEL

# ── Per-call timeout / retry budget (governed centrally) ──
# These were previously hardcoded (OpenRouter 180s/3 retries, DeepSeek
# 300s/3 retries). A single retried call could run ~990s and blow the parent
# pipeline budget. Now configurable and tightened; a `deadline` can further
# short-circuit runaway retries.
#
# P2-6: long-form writing uses max_tokens=8192, which can take well over the
# old 120s — bump the default per-call timeout to 300s to avoid false timeouts.
# The global budget is injected by content_platform/runtime.py via LLM_DEADLINE.
LLM_CALL_TIMEOUT = int(os.environ.get("LLM_CALL_TIMEOUT", "300"))
# Keep retries bounded so each call remains within the writer deadline.
LLM_MAX_RETRIES = int(os.environ.get("LLM_MAX_RETRIES", "1"))
# Extra finite retries specifically for an EMPTY content response (reasoning
# models occasionally burn budget and return content=""). These are bounded and
# cheap — they only fire when the last attempt returned no usable text, and are
# still short-circuited by the shared `deadline`.
LLM_EMPTY_RETRIES = int(os.environ.get("LLM_EMPTY_RETRIES", "1"))
# Absolute wall-clock deadline (epoch seconds) for the whole writer run.
# None when not injected (callers that don't pass `deadline=` and runtime
# doesn't set LLM_DEADLINE get no short-circuit — same as before P1-2).
LLM_DEADLINE = float(os.environ["LLM_DEADLINE"]) if os.environ.get("LLM_DEADLINE") else None


def resolve_model(model_id: str) -> str:
    """Resolve model alias to OpenRouter model ID."""
    return OPENROUTER_MODEL_ALIASES.get(model_id, model_id)


def call_openrouter(
    messages: list,
    model: str = None,
    temperature: float = 1.0,
    max_tokens: int = 4096,
    max_retries: int = None,
    deadline: Optional[float] = None,
) -> Optional[str]:
    """Call any model through OpenRouter API.

    Args:
        messages: List of dicts with 'role' and 'content' keys.
        model: OpenRouter model ID (e.g., "deepseek/deepseek-v4-flash", "openai/gpt-5.4").
        temperature: Sampling temperature.
        max_tokens: Maximum tokens in response.
        max_retries: Number of retry attempts on failure.

    Returns:
        Response text string, or None on failure.
    """
    model = model or "deepseek/deepseek-v4-flash"
    model = resolve_model(model)

    if max_retries is None:
        max_retries = LLM_MAX_RETRIES
    timeout = LLM_CALL_TIMEOUT

    url = f"{OPENROUTER_BASE}/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    # max_tokens=None -> omit the field entirely (generation runs to natural
    # completion instead of being hard-capped). DeepSeek reasoning models share
    # the budget between reasoning_content and content, so an explicit cap can
    # leave content EMPTY. Omitting lets the model finish writing.
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens

    # Disable reasoning for GPT-5.4 (writing/polishing — don't want hidden thinking)
    if "gpt-5.4" in model or "gpt-5.3" in model or "gpt-5." in model:
        payload["reasoning"] = {"effort": "none"}

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "HTTP-Referer": "https://github.com/openclaw/agents",
            "X-Title": "Content-Hub",
        },
    )

    for attempt in range(max_retries):
        if deadline is not None and time.time() > deadline:
            print(f"  ⚠️ OpenRouter deadline exceeded; aborting retries")
            return None
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
            raw_msg = data.get("choices", [{}])[0].get("message", {})
            content = raw_msg.get("content") or ""
            if content and content.strip():
                return content
            # ── Empty/whitespace-only content: treat as failed call and retry ──
            reasoning = raw_msg.get("reasoning_content") or ""
            finish = data.get("choices", [{}])[0].get("finish_reason", "")
            reason_note = "truncated-by-length" if finish == "length" else f"finish={finish or 'empty'}"
            print(f"  ⚠️ OpenRouter: EMPTY content (attempt {attempt+1}/{max_retries}, "
                  f"model={model}, {reason_note}, reasoning={len(reasoning)} tokens). "
                  f"Retrying empty response.")
            # Fall through to the end of the loop body, which sleeps and retries.
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            try:
                err_data = json.loads(body)
                err_msg = err_data.get("error", {}).get("message", body[:200])
            except json.JSONDecodeError:
                err_msg = body[:200]
            print(f"  ⚠️ OpenRouter attempt {attempt + 1}/{max_retries}: {e.code} {err_msg}")
            if e.code == 429:
                wait = (attempt + 1) * 15
                print(f"  Rate limited. Waiting {wait}s...")
                time.sleep(wait)
            elif e.code >= 500:
                wait = (attempt + 1) * 10
                print(f"  Server error. Waiting {wait}s...")
                time.sleep(wait)
            else:
                return None  # Don't retry on 4xx errors other than 429
        except Exception as e:
            print(f"  ⚠️ OpenRouter attempt {attempt + 1}/{max_retries}: {e}")
            if attempt < max_retries - 1:
                wait = (attempt + 1) * 10
                time.sleep(wait)
    return None


def call_deepseek_direct(
    messages: list,
    temperature: float = 1.0,
    max_tokens: int = None,
    max_retries: int = None,
    deadline: Optional[float] = None,
    model: str = None,
) -> Optional[str]:
    """Call DeepSeek Chat directly via their API."""
    model = (model or DEEPSEEK_MODEL).strip().lower()
    legacy_model_ids = {
        "deepseek-v4-flash": "deepseek-flash",
        "deepseek/deepseek-v4-flash": "deepseek-flash",
        "deepseek-v4-flash-vision-exp": "deepseek-flash",
        "google/gemini-3.7-flash": "deepseek-flash",
        "openai/gpt-5.4": "deepseek-flash",
        "openai-codex/gpt-5.4": "deepseek-flash",
    }
    model = legacy_model_ids.get(model, model)
    if model not in DEEPSEEK_MODELS:
        raise ValueError(
            f"Unsupported DeepSeek API model '{model}'. "
            f"Choose one of: {', '.join(sorted(DEEPSEEK_MODELS))}."
        )
    if not DEEPSEEK_API_KEY:
        print("  ❌ DEEPSEEK_API_KEY is not configured")
        return None
    if max_retries is None:
        max_retries = LLM_MAX_RETRIES
    timeout = LLM_CALL_TIMEOUT

    url = f"{DEEPSEEK_BASE}/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "thinking": {"type": "enabled"},
        "reasoning_effort": os.environ.get("DEEPSEEK_REASONING_EFFORT", "low"),
    }
    # NOTE: 不写入 max_tokens 字段。DeepSeek 直连统一不限制输出长度，
    # 让 reasoning + content 各自自然跑完，避免截断/空卡。
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        },
    )
    for attempt in range(max_retries):
        if deadline is not None and time.time() > deadline:
            print(f"  ⚠️ DeepSeek deadline exceeded; aborting retries")
            return None
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
            raw_msg = data.get("choices", [{}])[0].get("message", {})
            content = raw_msg.get("content") or ""
            reasoning = raw_msg.get("reasoning_content") or ""
            finish = data.get("choices", [{}])[0].get("finish_reason", "")
            if content and content.strip():
                return content
            # ── Empty/whitespace-only content ──
            # No usable text to hand back. Whether or not finish_reason is
            # "length", an empty content is a FAILED call: we log the model,
            # phase, reasoning length and retry a finite number of times rather
            # than return an empty string (which callers would then .strip()
            # into a misleading "" or raise NoneType.strip).
            phase = "<unknown>"
            if messages:
                last_user = next((m.get("content") for m in reversed(messages)
                                  if m.get("role") == "user" and m.get("content")), "")
                if last_user:
                    phase = (str(last_user)[:40].replace("\n", " ")) if isinstance(last_user, str) else phase
            reason_note = "truncated-by-length" if finish == "length" else f"finish={finish or 'empty'}"
            print(f"  ⚠️ DeepSeek API: EMPTY content (attempt {attempt+1}/{max_retries}, "
                  f"model={model}, phase='{phase}', {reason_note}, "
                  f"reasoning={len(reasoning)} tokens). Retrying empty response.")
            # Fall through to the end of the loop body, which sleeps and retries.
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            try:
                err_data = json.loads(body)
                err_msg = err_data.get("error", {}).get("message", body[:200])
            except json.JSONDecodeError:
                err_msg = body[:200]
            print(f"  ⚠️ DeepSeek API attempt {attempt + 1}/{max_retries}: {e.code} {err_msg}")
            if e.code == 429:
                wait = (attempt + 1) * 15
                print(f"  Rate limited. Waiting {wait}s...")
                time.sleep(wait)
            elif e.code >= 500:
                wait = (attempt + 1) * 10
                print(f"  Server error. Waiting {wait}s...")
                time.sleep(wait)
            else:
                return None
        except Exception as e:
            print(f"  ⚠️ DeepSeek API attempt {attempt + 1}/{max_retries}: {e}")
            if attempt < max_retries - 1:
                wait = (attempt + 1) * 10
                time.sleep(wait)
    # ── Bounded empty-content retries ──
    # If the primary retry window produced no content, give the reasoning model
    # a finite number of extra chances. Aborts immediately once the shared
    # deadline is reached. Explicitly returns None (empty is never returned).
    for rtry in range(1, LLM_EMPTY_RETRIES + 1):
        if deadline is not None and time.time() > deadline:
            print(f"  ⚠️ DeepSeek deadline exceeded during empty retry; aborting")
            return None
        print(f"  🔁 DeepSeek API empty-content retry {rtry}/{LLM_EMPTY_RETRIES}...")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
            raw_msg = data.get("choices", [{}])[0].get("message", {})
            content = raw_msg.get("content") or ""
            if content and content.strip():
                return content
            reasoning = raw_msg.get("reasoning_content") or ""
            print(f"  ⚠️ DeepSeek API: still empty after retry {rtry} "
                  f"(reasoning={len(reasoning)} tokens)")
        except Exception as e:
            print(f"  ⚠️ DeepSeek API empty retry {rtry}/{LLM_EMPTY_RETRIES}: {e}")
    return None


def call_model(
    messages: list,
    temperature: float = 1.0,
    max_tokens: int = 4096,
    model: str = None,
    deadline: Optional[float] = None,
) -> Optional[str]:
    """Call the configured model.

    Calls only the DeepSeek API. Provider and model failures remain explicit.

    Args:
        messages: List of dicts with 'role' and 'content' keys.
        temperature: Sampling temperature (0.0-1.0).
        max_tokens: Maximum tokens in response.
        model: Model identifier. If None, uses DEFAULT_WRITING_MODEL.

    Returns:
        Response text string, or None on failure.
    """
    # Use the globally-injected deadline when no explicit deadline is passed.
    if deadline is None:
        deadline = LLM_DEADLINE
    if deadline is not None and time.time() > deadline:
        print("⏱️ deadline exceeded, aborting remaining LLM calls")
        return None
    model = model or DEFAULT_WRITING_MODEL

    result = call_deepseek_direct(
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        deadline=deadline,
        model=model or DEFAULT_WRITING_MODEL,
    )
    # Defensive: never leak an empty/whitespace string upstream — callers must
    # be able to rely on `if not result` and on result.strip() being safe.
    return result if (result and result.strip()) else None


def _is_openrouter_strong_model(model: str) -> bool:
    """Legacy helper retained for imports; call_model always routes direct."""
    return False
