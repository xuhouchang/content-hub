#!/usr/bin/env python3
"""
Centralized model configuration for the collector pipeline.

All scripts should import `get_model()` from here instead of defining
their own defaults or referencing environment variables directly.

Usage:
    from lib.models import get_model
    model = get_model("writing")

Environment variable overrides MODEL_WRITING, MODEL_POLISH, MODEL_TOPIC,
MODEL_CASE, MODEL_FILTER, MODEL_SYNTHESIS, or MODEL_FRAMEWORK may select one
of the models supported by the DeepSeek API. Legacy DeepSeek V4 Flash names
are normalized to the current ``deepseek-flash`` API model ID.
"""

import os
from typing import Optional

DEEPSEEK_FLASH = "deepseek-flash"
DEFAULT_MODEL = os.environ.get("DEEPSEEK_MODEL", DEEPSEEK_FLASH).strip() or DEEPSEEK_FLASH
_MODELS = {
    purpose: os.environ.get(
        f"MODEL_{purpose.upper()}",
        os.environ.get("WRITING_MODEL", DEFAULT_MODEL) if purpose == "writing" else DEFAULT_MODEL,
    )
    for purpose in ("writing", "polish", "topic", "case", "filter", "synthesis", "framework")
}

_MODEL_ALIASES = {
    "deepseek-v4-flash": DEEPSEEK_FLASH,
    "deepseek/deepseek-v4-flash": DEEPSEEK_FLASH,
    "deepseek-v4-flash-vision-exp": DEEPSEEK_FLASH,
    "google/gemini-3.7-flash": DEEPSEEK_FLASH,
    "openai/gpt-5.4": DEEPSEEK_FLASH,
    "openai-codex/gpt-5.4": DEEPSEEK_FLASH,
    "deepseek/deepseek-v4-pro": "deepseek-v4-pro",
}


def get_model(purpose: str) -> str:
    """Get the configured model for a given purpose.

    Args:
        purpose: One of "writing", "polish", "topic", "case", "filter",
                 "synthesis", "framework".

    Returns:
        DeepSeek API model identifier.
    """
    model = _MODELS.get(purpose, DEEPSEEK_FLASH)
    return _MODEL_ALIASES.get(model.strip().lower(), model.strip())


def _get_all_purposes():
    """Return all model configs as dict (for debugging)."""
    return dict(_MODELS)
