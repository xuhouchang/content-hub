#!/usr/bin/env python3
"""
LLM call encapsulation for the collector pipeline.

Unified call_model() that routes all models through OpenRouter API.
- "deepseek-chat": deepseek/deepseek-chat on OpenRouter
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
    "deepseek-chat": "deepseek/deepseek-chat",
    "openai-codex/gpt-5.4": "openai/gpt-5.4",
}

# ── Default Writing Model ──
# Use "deepseek-chat" for DeepSeek (cheaper, fine for filtration/clustering)
# Use "openai/gpt-5.4" for actual writing
DEFAULT_WRITING_MODEL = os.environ.get("WRITING_MODEL", "openai/gpt-5.4")


def resolve_model(model_id: str) -> str:
    """Resolve model alias to OpenRouter model ID."""
    return MODEL_ALIASES.get(model_id, model_id)


def call_openrouter(
    messages: list,
    model: str = None,
    temperature: float = 1.0,
    max_tokens: int = 4096,
    max_retries: int = 3,
) -> Optional[str]:
    """Call any model through OpenRouter API.

    Args:
        messages: List of dicts with 'role' and 'content' keys.
        model: OpenRouter model ID (e.g., "deepseek/deepseek-chat", "openai/gpt-5.4").
        temperature: Sampling temperature.
        max_tokens: Maximum tokens in response.
        max_retries: Number of retry attempts on failure.

    Returns:
        Response text string, or None on failure.
    """
    model = model or "deepseek/deepseek-chat"
    model = resolve_model(model)

    url = f"{OPENROUTER_BASE}/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

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
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read().decode())
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            if content:
                return content
            # Handle empty content (e.g., tool calls not supported)
            finish = data.get("choices", [{}])[0].get("finish_reason", "")
            if finish == "length":
                return content  # Return partial
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
    max_tokens: int = 8192,
    max_retries: int = 3,
) -> Optional[str]:
    """Call DeepSeek Chat directly via their API."""
    url = f"{DEEPSEEK_BASE}/chat/completions"
    payload = {
        "model": "deepseek-chat",
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        },
    )
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = json.loads(resp.read().decode())
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            if content:
                return content
            finish = data.get("choices", [{}])[0].get("finish_reason", "")
            if finish == "length":
                return content
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
    return None


def call_model(
    messages: list,
    temperature: float = 1.0,
    max_tokens: int = 4096,
    model: str = None,
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
    model = model or DEFAULT_WRITING_MODEL

    # ── 固化模型路由规则（不依赖 Skills / agent prompt）──
    # 写作 & 润色（openai/gpt-5.4）：走 OpenRouter，GPT-5.4 直调
    # 素材过滤 & 配图描述（deepseek-chat）：走 DeepSeek 直连（便宜）
    # ─────────────────────────────────────────
    if model and ("openai" in model or "gpt" in model or model == DEFAULT_WRITING_MODEL):
        # GPT 模型 → 走 OpenRouter
        result = call_openrouter(messages, model=model, temperature=temperature, max_tokens=max_tokens)
        if result:
            return result
        print("  OpenRouter failed, falling back to DeepSeek direct...")
        return call_deepseek_direct(messages, temperature=temperature, max_tokens=max_tokens)

    # DeepSeek / 其他模型 → 走 DeepSeek 直连优先
    result = call_deepseek_direct(messages, temperature=temperature, max_tokens=max_tokens)
    if result:
        return result
    print("  DeepSeek direct failed, falling back to OpenRouter...")
    return call_openrouter(messages, model=model, temperature=temperature, max_tokens=max_tokens)
