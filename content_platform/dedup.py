"""Selection-boundary dedup shared across content types.

Cross-type dedup shares ONLY the normalized source URL (see
``normalize.urls.normalized_url_key``) and this content-hash registry; per-format
topic/cluster diversity stays in the article/case ledgers.
"""

from __future__ import annotations

from pathlib import Path

# Guarded so importing the pools still works if lib is unavailable.
try:
    from lib import is_content_duplicate
except ImportError:  # pragma: no cover - only if the package layout changes
    is_content_duplicate = None

# Only substantive bodies are trusted for content-level dedup; stubs would
# otherwise collide on a constant hash. Matches runtime._MIN_CONTENT_DEDUP_CHARS.
MIN_CONTENT_DEDUP_CHARS = 200


def content_hash_seen(material: dict, workspace_dir: Path | None) -> str | None:
    """Return the original source URL if this body was already published.

    Returns ``None`` when the material carries no hash, is too short to trust,
    the registry is unavailable, or the hash is unknown. An explicit
    ``workspace_dir`` is required so tests and multi-workspace runs never read
    another workspace's history.
    """
    if is_content_duplicate is None or workspace_dir is None:
        return None
    content_hash = material.get("content_hash", "")
    if not content_hash:
        return None
    body = material.get("content_text") or material.get("content") or ""
    if len(body) < MIN_CONTENT_DEDUP_CHARS:
        return None
    return is_content_duplicate(content_hash, workspace_dir=workspace_dir)
