"""Build article candidate pool from curated materials.

M2 adds evidence-led candidacy: a candidate must carry checkable evidence
(facts/data or implementation detail) to enter the pool. The evidence fields are
also used for explainable ranking, instead of a new opaque numeric cutoff.
"""

import re
from pathlib import Path

from content_platform.dedup import content_hash_seen
from content_platform.recent_topics import recent_cluster_ids

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

# Fact/data signals: explicit numbers, percentages, monetary amounts, metrics.
_FACT_PATTERN = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|％|percent|percentage points?|bps|"
    r"x|times|亿|万|千|百万|billion|million|thousand|k\b|"
    r"users?|customers?|tickets?|hours?|days?|weeks?|months?|"
    r"人|家|个|倍)|\$\s?\d+|\d{2,}",
    re.IGNORECASE,
)

# Implementation-detail signals: the material describes something actually done,
# not just an opinion about the direction of travel.
_IMPLEMENTATION_MARKERS = (
    "implemented", "implementation", "deployed", "deployment", "rolled out",
    "rollout", "rolled-out", "pilot", "piloted", "migrated", "migration",
    "automated", "automation rate", "measured", "metric", "cost savings",
    "saved", "reduced", "increased", "case study", "after go live", "go-live",
    "上线", "部署", "落地", "实施", "试点", "迁移", "接手", "接入", "改造",
    "节省", "提效", "降本", "复盘", "落地实践",
)


def _used_cluster_ids(days: int = 7, workspace_dir: Path | None = None) -> set[str]:
    """Read recently used article clusters from the article-only ledger."""
    root = Path(workspace_dir) if workspace_dir else Path(__file__).resolve().parent.parent.parent
    return recent_cluster_ids(root, "article", days=days)


def _recent_cluster_ids(topic_memory: dict) -> set[str]:
    """Legacy: cluster ids from topic_memory (kept for backward compatibility)."""
    return {
        cluster_id
        for output in topic_memory.get("recent_outputs", [])
        for cluster_id in output.get("cluster_ids", [])
    }


def _candidate_text(material: dict) -> str:
    return (material.get("summary") or "") + " " + (
        material.get("content_text") or material.get("content") or ""
    )


def extract_candidate_evidence(material: dict) -> dict:
    """Return the evidence record used for candidacy and explainable ranking."""
    text = _candidate_text(material)[:6000]
    facts = _FACT_PATTERN.findall(text)
    implementation_markers = [
        marker for marker in _IMPLEMENTATION_MARKERS if marker.lower() in text.lower()
    ]
    quality = material.get("quality") or {}
    content_chars = quality.get("content_chars")
    if not isinstance(content_chars, int):
        content_chars = len(material.get("content_text") or material.get("content") or "")
    return {
        "original_source": {
            "url": material.get("canonical_url") or material.get("url") or "",
            "source_name": material.get("source_name", ""),
            "source_type": material.get("source_type", ""),
        },
        "checkable_facts": list(dict.fromkeys(facts))[:8],
        "implementation_details": implementation_markers[:5],
        "body_completeness": {
            "content_chars": content_chars,
            "min_required": MIN_CONTENT_CHARS,
            "complete": content_chars >= MIN_CONTENT_CHARS,
        },
        "scoring_confidence": material.get("score_confidence", "unknown"),
    }


def _has_checkable_evidence(evidence: dict) -> bool:
    return bool(evidence["checkable_facts"]) or bool(evidence["implementation_details"])


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


def build_article_pool(
    materials: list[dict],
    topic_memory: dict,
    workspace_dir: Path | None = None,
) -> dict:
    """Build the candidate pool with an evidence record and rejection reasons.

    Diversity is checked only against the article-only ledger in
    ``workspace_dir`` (the shared cross-type view stays URL/content-hash based).
    """
    recent_clusters = _recent_cluster_ids(topic_memory) | _used_cluster_ids(
        days=7, workspace_dir=workspace_dir
    )
    candidates: list[dict] = []
    rejected: list[dict] = []

    for material in materials:
        title = material.get("title", "") or ""
        if material.get("editorial_fit_score", 0.0) < PRIMARY_TOPIC_THRESHOLD:
            rejected.append({"title": title, "reason": "below_editorial_threshold"})
            continue
        if material.get("quality", {}).get("content_chars", 0) < MIN_CONTENT_CHARS:
            rejected.append({"title": title, "reason": "insufficient_body"})
            continue
        if material.get("dedup", {}).get("cluster_id") in recent_clusters:
            rejected.append({"title": title, "reason": "recent_cluster"})
            continue

        # ── Cross-type content-hash dedup at the selection boundary ──
        # A reprint with a different URL but the same body was already
        # published, so it must not be selected again.
        duplicate_of = content_hash_seen(material, workspace_dir)
        if duplicate_of:
            rejected.append({
                "title": title,
                "reason": "recent_content_hash",
                "duplicate_of": str(duplicate_of),
            })
            continue

        # ── Stage 2: Exclude AI safety topics (with exception check) ──
        if _is_ai_safety_topic(material):
            rejected.append({"title": title, "reason": "ai_safety_topic"})
            continue

        evidence = extract_candidate_evidence(material)
        if not _has_checkable_evidence(evidence):
            # A pure opinion piece must not enter the pool on length/score alone.
            rejected.append({"title": title, "reason": "no_checkable_evidence"})
            continue

        enriched = dict(material)
        enriched["candidate_evidence"] = evidence
        candidates.append(enriched)

    return {"candidates": candidates, "rejected": rejected}
