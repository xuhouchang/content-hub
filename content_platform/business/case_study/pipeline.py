from pathlib import Path

from content_platform.business.case_study.ranker import rank_case_candidates
from content_platform.datasets.case_pool import build_case_pool
from content_platform.recent_topics import recent_source_urls
from content_platform.storage.json_store import write_json


def run_case_study_pipeline(
    date_str: str,
    workspace_dir: Path,
    materials: list[dict],
    topic_memory: dict | None = None,
) -> dict:
    memory = topic_memory or {"recent_outputs": []}
    pool = build_case_pool(materials, topic_memory=memory)
    candidates = pool["candidates"]
    ranked = rank_case_candidates(candidates) if candidates else []
    # ── URL-level dedup: drop candidates whose source URL was used recently
    #    by either content format. Topic-diversity histories remain separate.
    recent_urls = recent_source_urls(workspace_dir, days=30)
    if recent_urls:
        deduped = []
        for c in ranked:
            u = (
                c.get("canonical_url")
                or c.get("url")
                or c.get("normalized_url", "")
            ).rstrip("/").lower()
            if u and u in recent_urls:
                print(f"  ⏭️  Case skip (same source URL used recently): {str(c.get('title'))[:50]}")
                continue
            deduped.append(c)
        ranked = deduped
    selected = ranked[0] if ranked else None
    dataset_dir = workspace_dir / "platform" / "datasets" / date_str
    dataset_dir.mkdir(parents=True, exist_ok=True)
    selection_file = dataset_dir / "case_selection.json"
    materials_file = dataset_dir / "case_materials.json"
    status = "success" if selected else "failed"
    write_json(selection_file, {"candidates": ranked, "selected": selected, "status": status})
    write_json(materials_file, [selected] if selected else [])
    return {
        "candidates": ranked,
        "selected": selected,
        "selection_file": str(selection_file),
        "materials_file": str(materials_file),
        "status": status,
    }
