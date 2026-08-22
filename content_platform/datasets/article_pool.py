"""Build article candidate pool from curated materials."""

import datetime
import json
import re
from pathlib import Path

PRIMARY_TOPIC_THRESHOLD = 0.65
MIN_CONTENT_CHARS = 800

# AI safety exclusion rules
# Topics matching these patterns are excluded UNLESS an exception pattern matches.
AI_SAFETY_KEYWORDS = [
    r'ai\s*(?:safety|alignment|governance|regulation|interpretability|explainability)',
    r'red\s*teaming',
    r'ai\s*(?:bias|fairness|discrimination)',
    r'(?:existential|catastrophic)\s*risk',
    r'ai\s*(?:risk|harm)\s*frameworks?',
    r'(?:model|ai)\s*(?:safety|alignment)\s*(?:framework|research)',
]

# Exception patterns — if ANY matches, the material is ALLOWED even if
# AI_SAFETY_KEYWORDS also matches.
# This is a narrow list for genuinely surprising/deep AI safety content.
AI_SAFETY_EXCEPTIONS = [
    # Agent-to-agent real attack (with experimental evidence)
    r'agent\s*(?:attack|attacking|compromise|hack)\s*(?:each|another|other)',
    # Autonomous self-replication / unexpected emergent behavior with proof
    r'(?:self\s*-\s*replicat|emergent.*behavior|unexpected.*capability).*experiment',
    # Verified real-world incident with detailed technical report
    r'real\s*-?world\s+incident.*(?:technical|detailed|report)',
]

# _recent_topics.json is written by write_article.py after each publish
_RECENT_TOPICS_FILE = (
    Path(__file__).resolve().parent.parent.parent
    / "wechat-articles"
    / "_recent_topics.json"
)


def _used_cluster_ids(days: int = 7) -> set[str]:
    """Read recently published articles from _recent_topics.json and extract cluster_ids.

    Each entry in _recent_topics.json may have a 'cluster_ids' field listing
    the dedup cluster_id(s) consumed by that article.  Falls back to extracting
    a cluster_id from the output_dir name if cluster_ids is absent.
    """
    if not _RECENT_TOPICS_FILE.exists():
        return set()
    try:
        entries = json.loads(_RECENT_TOPICS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    cutoff = datetime.date.today() - datetime.timedelta(days=days)
    used: set[str] = set()
    for e in entries:
        try:
            d = datetime.date.fromisoformat(e.get("date", "2000-01-01"))
        except ValueError:
            continue
        if d < cutoff:
            continue
        cids = e.get("cluster_ids", [])
        if cids:
            used.update(cids)
    return used


def _recent_cluster_ids(topic_memory: dict) -> set[str]:
    """Legacy: cluster ids from topic_memory (kept for backward compatibility)."""
    return {
        cluster_id
        for output in topic_memory.get("recent_outputs", [])
        for cluster_id in output.get("cluster_ids", [])
    }


def build_article_pool(materials: list[dict], topic_memory: dict) -> dict:
    # Combine legacy in-memory cluster ids with persistent recent-topics data
    recent_clusters = _recent_cluster_ids(topic_memory) | _used_cluster_ids(days=7)
    candidates = [
        material
        for material in materials
        if material.get("editorial_fit_score", 0.0) >= PRIMARY_TOPIC_THRESHOLD
        and material.get("quality", {}).get("content_chars", 0) >= MIN_CONTENT_CHARS
        and material.get("dedup", {}).get("cluster_id") not in recent_clusters
    ]

    # ── Stage 2: Exclude AI safety topics (with exception check) ──
    def _is_ai_safety_topic(material: dict) -> bool:
        """Check if material is an AI safety topic, with narrow exception list."""
        text_to_check = (
            (material.get("title", "") or "") + " " +
            (material.get("summary", "") or "") + " " +
            (material.get("canonical_url", "") or "") + " " +
            (material.get("url", "") or "") + " " +
            (material.get("content", "") or "")[:2000]
        ).lower()

        # Check exceptions first (narrow allowlist)
        for exc_pattern in AI_SAFETY_EXCEPTIONS:
            if re.search(exc_pattern, text_to_check, re.IGNORECASE):
                return False  # Exception applies — do NOT exclude

        # Check main exclusion patterns
        for pattern in AI_SAFETY_KEYWORDS:
            if re.search(pattern, text_to_check, re.IGNORECASE):
                return True  # AI safety topic — exclude

        return False

    excluded_by_safety = [m for m in candidates if _is_ai_safety_topic(m)]
    if excluded_by_safety:
        print(f"  🚫 Excluded {len(excluded_by_safety)} AI safety candidate(s):")
        for m in excluded_by_safety[:5]:
            print(f"     • {m.get('title', '')[:60]}")
    candidates = [m for m in candidates if not _is_ai_safety_topic(m)]

    return {"candidates": candidates}
