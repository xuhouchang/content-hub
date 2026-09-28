"""Build case-study candidate pool from curated materials."""

from pathlib import Path

from content_platform.dedup import content_hash_seen
from content_platform.recent_topics import recent_cluster_ids

CASE_DETAIL_THRESHOLD = 0.6
PRIMARY_TOPIC_THRESHOLD = 0.65


def _used_cluster_ids(days: int = 7, workspace_dir: Path | None = None) -> set[str]:
    """Read recently used case-study clusters from the case-only ledger."""
    root = Path(workspace_dir) if workspace_dir else Path(__file__).resolve().parent.parent.parent
    return recent_cluster_ids(root, "case", days=days)


def _recent_cluster_ids(topic_memory: dict) -> set[str]:
    """Legacy: cluster ids from topic_memory (kept for backward compatibility)."""
    return {
        cluster_id
        for output in topic_memory.get("recent_outputs", [])
        for cluster_id in output.get("cluster_ids", [])
    }


def build_case_pool(
    materials: list[dict],
    topic_memory: dict,
    workspace_dir: Path | None = None,
) -> dict:
    """Build the case pool, comparing topics ONLY against case history.

    Article diversity is judged separately; the cross-type guard shares only the
    normalized source URL and the content-hash registry.
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
        if material.get("execution_detail_score", 0.0) < CASE_DETAIL_THRESHOLD:
            rejected.append({"title": title, "reason": "insufficient_execution_detail"})
            continue
        if material.get("dedup", {}).get("cluster_id") in recent_clusters:
            rejected.append({"title": title, "reason": "recent_case_cluster"})
            continue
        # Cross-type content-hash dedup at the selection boundary.
        duplicate_of = content_hash_seen(material, workspace_dir)
        if duplicate_of:
            rejected.append({
                "title": title,
                "reason": "recent_content_hash",
                "duplicate_of": str(duplicate_of),
            })
            continue
        candidates.append(material)
    return {"candidates": candidates, "rejected": rejected}
