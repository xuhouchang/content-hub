"""Build case-study candidate pool from curated materials."""

from pathlib import Path
from content_platform.recent_topics import recent_cluster_ids

CASE_DETAIL_THRESHOLD = 0.6
PRIMARY_TOPIC_THRESHOLD = 0.65

def _used_cluster_ids(days: int = 7) -> set[str]:
    """Read recently used case-study clusters from the case-only ledger."""
    workspace_dir = Path(__file__).resolve().parent.parent.parent
    return recent_cluster_ids(workspace_dir, "case", days=days)


def _recent_cluster_ids(topic_memory: dict) -> set[str]:
    """Legacy: cluster ids from topic_memory (kept for backward compatibility)."""
    return {
        cluster_id
        for output in topic_memory.get("recent_outputs", [])
        for cluster_id in output.get("cluster_ids", [])
    }


def build_case_pool(materials: list[dict], topic_memory: dict) -> dict:
    recent_clusters = _recent_cluster_ids(topic_memory) | _used_cluster_ids(days=7)
    candidates = [
        material
        for material in materials
        if material.get("editorial_fit_score", 0.0) >= PRIMARY_TOPIC_THRESHOLD
        and material.get("execution_detail_score", 0.0) >= CASE_DETAIL_THRESHOLD
        and material.get("dedup", {}).get("cluster_id") not in recent_clusters
    ]
    return {"candidates": candidates}
