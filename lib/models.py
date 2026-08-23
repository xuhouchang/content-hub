#!/usr/bin/env python3
"""
Centralized model configuration for the collector pipeline.

All scripts should import `get_model()` from here instead of defining
their own defaults or referencing environment variables directly.

Usage:
    from lib.models import get_model
    model = get_model("writing")

Environment variable overrides:
    MODEL_WRITING      — 公众号写作 (default: google/gemini-3.7-flash)
    MODEL_POLISH       — 润色       (default: google/gemini-3.7-flash)
    MODEL_CASE         — 案例拆解   (default: deepseek/deepseek-v4-flash)
    MODEL_FILTER       — 素材过滤   (default: deepseek/deepseek-v4-flash)
    MODEL_SYNTHESIS    — 周度整合   (default: deepseek/deepseek-v4-flash)
    MODEL_FRAMEWORK    — 框架文章   (default: deepseek/deepseek-v4-flash)

写作/润色走强模型（Gemini 3.7 Flash），打分/选题/过滤等批量环节走便宜模型（DeepSeek）。

Legacy:
    WRITING_MODEL env var is still respected as a blanket fallback.
    DEFAULT_WRITING_MODEL in lib/llm.py remains for backward compat
    but new code should prefer get_model().
"""

import os
from typing import Optional

_MODELS = {
    # 写作/润色用强模型（Gemini 3.7 Flash），保证文章质量
    "writing":    os.environ.get("MODEL_WRITING",    "google/gemini-3.7-flash"),
    "polish":     os.environ.get("MODEL_POLISH",     "google/gemini-3.7-flash"),
    # 打分/选题/过滤等批量环节用便宜模型
    "case":       os.environ.get("MODEL_CASE",       "deepseek/deepseek-v4-flash"),
    "filter":     os.environ.get("MODEL_FILTER",     "deepseek/deepseek-v4-flash"),
    "synthesis":  os.environ.get("MODEL_SYNTHESIS",  "deepseek/deepseek-v4-flash"),
    "framework":  os.environ.get("MODEL_FRAMEWORK",  "deepseek/deepseek-v4-flash"),
}


def get_model(purpose: str) -> str:
    """Get the configured model for a given purpose.

    Args:
        purpose: One of "writing", "polish", "case", "filter",
                 "synthesis", "framework".

    Returns:
        Full model identifier string (e.g., "openai/gpt-5.4").
    """
    return _MODELS.get(purpose, os.environ.get("WRITING_MODEL", "openai/gpt-5.4"))


def _get_all_purposes():
    """Return all model configs as dict (for debugging)."""
    return dict(_MODELS)
