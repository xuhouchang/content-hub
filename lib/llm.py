#!/usr/bin/env python3
"""
LLM call encapsulation for the collector pipeline.

Unified call_model() that routes all models through OpenRouter API.
- "deepseek-v4-flash": deepseek/deepseek-v4-flash on OpenRouter
- "openai-codex/gpt-5.4" or "openai/gpt-5.4": OpenAI GPT-5.4 on OpenRouter
- Any other model identifier is passed as-is to OpenRouter.
"""

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

# ── OpenRouter Config ──
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "") or os.environ.get("LLM_API_KEY", "")
OPENROUTER_BASE = "https://openrouter.ai/api/v1"

# ── DeepSeek Direct Config ──
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", os.environ.get("LLM_API_KEY", ""))
DEEPSEEK_BASE = "https://api.deepseek.com/v1"

# ── Model Aliases ──
MODEL_ALIASES = {
    "deepseek-v4-flash": "deepseek/deepseek-v4-flash",
    "openai-codex/gpt-5.4": "openai/gpt-5.4",
}

# ── Default Writing Model ──
# Use "deepseek-v4-flash" for DeepSeek (cheaper, fine for filtration/clustering)
# Use "openai/gpt-5.4" for actual writing
DEFAULT_WRITING_MODEL = os.environ.get("WRITING_MODEL", "deepseek-v4-flash")

# ── Per-call timeout / retry budget (governed centrally) ──
# These were previously hardcoded (OpenRouter 180s/3 retries, DeepSeek
# 300s/3 retries). A single retried call could run ~990s and blow the parent
# pipeline budget. Now configurable and tightened; a `deadline` can further
# short-circuit runaway retries.
#
# P2-6: long-form writing uses max_tokens=8192, which can take well over the
# old 120s — bump the default per-call timeout to 300s to avoid false timeouts.
# P1-2: the global budget is injected by content_platform/runtime.py via the
# LLM_DEADLINE env var (absolute epoch-seconds). call_model() reads it as the
# default deadline so the OpenRouter->DeepSeek fallback short-circuits before
# blowing the 900s parent budget.
LLM_CALL_TIMEOUT = int(os.environ.get("LLM_CALL_TIMEOUT", "300"))
# Retries trimmed 3->2 (P0) and 2->1 (P1-2) so the worst case per call_model
# stays bounded: 1 attempt x 300s OpenRouter + 1 attempt x 300s DeepSeek = 600s
# worst, comfortably under the 900s parent and the LLM_DEADLINE budget. The
# OpenRouter->DeepSeek cross-provider fallback still provides redundancy.
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
    return MODEL_ALIASES.get(model_id, model_id)


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
) -> Optional[str]:
    """Call DeepSeek Chat directly via their API."""
    if max_retries is None:
        max_retries = LLM_MAX_RETRIES
    timeout = LLM_CALL_TIMEOUT

    url = f"{DEEPSEEK_BASE}/chat/completions"
    payload = {
        "model": "deepseek-v4-flash",
        "messages": messages,
        "temperature": temperature,
        # deepseek-v4-flash is a REASONING model: reasoning_content shares the
        # max_tokens budget with content, and reasoning length is unpredictable.
        # We deliberately OMIT max_tokens entirely (老板要求：不限制 Max token)，
        # so generation runs to natural completion instead of being hard-capped
        # and left with empty/truncated content.
        "reasoning_effort": "low",
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
            print(f"  ⚠️ DeepSeek Direct: EMPTY content (attempt {attempt+1}/{max_retries}, "
                  f"model=deepseek-v4-flash, phase='{phase}', {reason_note}, "
                  f"reasoning={len(reasoning)} tokens). Retrying empty response.")
            # Fall through to the end of the loop body, which sleeps and retries.
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            try:
                err_data = json.loads(body)
                err_msg = err_data.get("error", {}).get("message", body[:200])
            except json.JSONDecodeError:
                err_msg = body[:200]
            print(f"  ⚠️ DeepSeek Direct attempt {attempt + 1}/{max_retries}: {e.code} {err_msg}")
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
            print(f"  ⚠️ DeepSeek Direct attempt {attempt + 1}/{max_retries}: {e}")
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
        print(f"  🔁 DeepSeek Direct empty-content retry {rtry}/{LLM_EMPTY_RETRIES}...")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
            raw_msg = data.get("choices", [{}])[0].get("message", {})
            content = raw_msg.get("content") or ""
            if content and content.strip():
                return content
            reasoning = raw_msg.get("reasoning_content") or ""
            print(f"  ⚠️ DeepSeek Direct: still empty after retry {rtry} "
                  f"(reasoning={len(reasoning)} tokens)")
        except Exception as e:
            print(f"  ⚠️ DeepSeek Direct empty retry {rtry}/{LLM_EMPTY_RETRIES}: {e}")
    return None


def call_model(
    messages: list,
    temperature: float = 1.0,
    max_tokens: int = 4096,
    model: str = None,
    deadline: Optional[float] = None,
) -> Optional[str]:
    """Call the configured model.

    Priority: DeepSeek direct → OpenRouter fallback.

    Args:
        messages: List of dicts with 'role' and 'content' keys.
        temperature: Sampling temperature (0.0-1.0).
        max_tokens: Maximum tokens in response.
        model: Model identifier. If None, uses DEFAULT_WRITING_MODEL.

    Returns:
        Response text string, or None on failure.
    """
    # P1-2: use the globally-injected deadline (LLM_DEADLINE, set by
    # runtime.py) when no explicit deadline is passed. This makes the
    # per-attempt short-circuit in call_openrouter/call_deepseek_direct
    # actually fire instead of being dead code.
    if deadline is None:
        deadline = LLM_DEADLINE
    # R1: if the global budget is already exhausted, abort remaining LLM calls
    # immediately instead of spending the last seconds on a provider that will
    # also fail. This is the LLM stage's contribution to the auto-degrade so
    # the writer always finishes and publishes within the 900s parent budget.
    if deadline is not None and time.time() > deadline:
        print("⏱️ deadline exceeded, aborting remaining LLM calls")
        return None
    model = model or DEFAULT_WRITING_MODEL

    # ── 固化模型路由规则（不依赖 Skills / agent prompt）──
    # 强模型（openai/gpt 系、google/gemini 系）→ 走 OpenRouter；失败后 fallback DeepSeek 直连
    # DeepSeek 系模型（deepseek/deepseek-v4-flash 等）：只走 DeepSeek 直连（便宜）
    # 2016-08 修正：google/gemini-* 之前误落入 else 分支被硬编码成 DeepSeek 直连，
    # 导致配置为 Gemini 3.7 Flash 的实际请求全都打到了 deepseek-v4-flash。
    # 现在 Gemini 等 OpenRouter 托管的强模型走 OpenRouter，真正命中 Gemini。
    # ─────────────────────────────────────────
    if model and (_is_openrouter_strong_model(model)):
        # GPT / Gemini 系列 → 走 OpenRouter
        result = call_openrouter(messages, model=model, temperature=temperature, max_tokens=max_tokens, deadline=deadline)
        if result and result.strip():
            return result
        print("  OpenRouter failed, falling back to DeepSeek direct...")
        return call_deepseek_direct(messages, temperature=temperature, max_tokens=max_tokens, deadline=deadline)

    # DeepSeek / 其他模型 → 只走 DeepSeek 直连，不 fallback 到 OpenRouter
    result = call_deepseek_direct(messages, temperature=temperature, max_tokens=max_tokens, deadline=deadline)
    # Defensive: never leak an empty/whitespace string upstream — callers must
    # be able to rely on `if not result` and on result.strip() being safe.
    return result if (result and result.strip()) else None


def _is_openrouter_strong_model(model: str) -> bool:
    """Return True if ``model`` should be routed through OpenRouter.

    Strong writing/polish/topic models (OpenAI GPT, Google Gemini) are hosted
    by OpenRouter; cheap/models available on DeepSeek direct stay on DeepSeek.
    """
    model_l = (model or "").lower()
    return (
        "openai/" in model_l or "gpt" in model_l
        or "google/" in model_l or "gemini" in model_l
    )
