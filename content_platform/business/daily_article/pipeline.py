"""Daily article pipeline: pick N distinct clusters, generate N articles."""

from pathlib import Path

from content_platform.business.daily_article.topic_planner import pick_top_n_clusters
from content_platform.datasets.article_pool import build_article_pool
from content_platform.storage.json_store import write_json


def run_daily_article_pipeline(
    date_str: str,
    workspace_dir: Path,
    materials: list[dict],
    topic_memory: dict | None = None,
    article_count: int = 2,
) -> dict:
    """Run article pipeline, selecting up to `article_count` distinct-topic clusters.

    Returns a dict with:
        - candidates (full candidate list)
        - selections (list of selected materials, one per cluster)
        - materials_files (list of paths to individual per-article materials JSONs)
        - selection_file
        - status
    """
    memory = topic_memory or {"recent_outputs": []}
    pool = build_article_pool(materials, topic_memory=memory)
    candidates = pool["candidates"]
    dataset_dir = workspace_dir / "platform" / "datasets" / date_str
    dataset_dir.mkdir(parents=True, exist_ok=True)
    selection_file = dataset_dir / "article_selection.json"
    materials_file = dataset_dir / "article_materials.json"

    if not candidates:
        write_json(selection_file, {"candidates": [], "selected": None, "status": "failed"})
        write_json(materials_file, [])
        return {
            "candidates": [],
            "selections": [],
            "materials_files": [],
            "selection_file": str(selection_file),
            "materials_file": str(materials_file),
            "status": "failed",
        }

    selections = pick_top_n_clusters(candidates, n=article_count)
    if not selections:
        write_json(selection_file, {"candidates": candidates, "selected": None, "status": "failed"})
        write_json(materials_file, [])
        return {
            "candidates": candidates,
            "selections": [],
            "materials_files": [],
            "selection_file": str(selection_file),
            "materials_file": str(materials_file),
            "status": "failed",
        }

    # Write master selection file with all picks
    write_json(selection_file, {
        "candidates": candidates,
        "selected": selections,
        "status": "success",
        "article_count": len(selections),
    })

    # Write individual materials files for each article (same format as before)
    materials_files = []
    for i, sel in enumerate(selections):
        fname = f"article_materials_{i+1}.json"
        fpath = dataset_dir / fname
        write_json(fpath, [sel])
        materials_files.append(str(fpath))

    # Backwards-compat: also write the first selection to the old path
    write_json(materials_file, [selections[0]])

    return {
        "candidates": candidates,
        "selections": selections,
        "materials_files": materials_files,
        "selection_file": str(selection_file),
        "materials_file": str(materials_file),
        "status": "success",
    }
