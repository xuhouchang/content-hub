"""Build case-study candidate pool from curated materials."""

import datetime
import json
from pathlib import Path

CASE_DETAIL_THRESHOLD = 0.6
PRIMARY_TOPIC_THRESHOLD = 0.65

# _recent_topics.json is written by write_article.py after each publish
_RECENT_TOPICS_FILE = (
    Path(__file__).resolve().parent.parent.parent
    / "wechat-articles"
    / "_recent_topics.json"
)


def _used_cluster_ids(days: int = 7) -> set[str]:
    """Read recently published articles from _recent_topics.json and extract cluster_ids."""
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
