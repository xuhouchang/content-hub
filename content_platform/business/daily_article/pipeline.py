"""Daily article pipeline: pick N distinct clusters, generate N articles."""

from pathlib import Path

from content_platform.business.daily_article.topic_planner import select_candidates
from content_platform.datasets.article_pool import build_article_pool
from content_platform.storage.json_store import write_json


def _count_rejections(pool_rejected: list[dict], decisions: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for item in pool_rejected:
        reason = item.get("reason", "unknown")
        counts[reason] = counts.get(reason, 0) + 1
    for decision in decisions:
        if decision.get("decision") == "rejected":
            reason = decision.get("reason", "unknown")
            counts[reason] = counts.get(reason, 0) + 1
    return counts


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
        - rejected / rejection_counts (evidence and diversity rejections)
        - decisions (per-candidate selection decisions and reasons)
        - referenced_history (recent article topics the decisions consulted)
        - selections (list of selected materials, one per cluster)
        - materials_files (list of paths to individual per-article materials JSONs)
        - selection_file
        - status
        - model_available (whether the semantic topic model responded)
    """
    memory = topic_memory or {"recent_outputs": []}
    pool = build_article_pool(materials, topic_memory=memory)
    candidates = pool["candidates"]
    pool_rejected = pool.get("rejected", [])
    dataset_dir = workspace_dir / "platform" / "datasets" / date_str
    dataset_dir.mkdir(parents=True, exist_ok=True)
    selection_file = dataset_dir / "article_selection.json"
    materials_file = dataset_dir / "article_materials.json"

    if not candidates:
        rejection_counts = _count_rejections(pool_rejected, [])
        write_json(selection_file, {
            "candidates": [],
            "selected": None,
            "decisions": [],
            "rejection_counts": rejection_counts,
            "referenced_history": [],
            "model_available": True,
            "status": "failed",
        })
        write_json(materials_file, [])
        return {
            "candidates": [],
            "rejected": pool_rejected,
            "rejection_counts": rejection_counts,
            "decisions": [],
            "referenced_history": [],
            "selections": [],
            "materials_files": [],
            "selection_file": str(selection_file),
            "materials_file": str(materials_file),
            "model_available": True,
            "status": "failed",
        }

    selection = select_candidates(candidates, n=article_count)
    selections = selection["selected"]
    decisions = selection["decisions"]
    referenced_history = selection["referenced_history"]
    rejection_counts = _count_rejections(pool_rejected, decisions)

    if not selections:
        write_json(selection_file, {
            "candidates": candidates,
            "selected": None,
            "decisions": decisions,
            "rejection_counts": rejection_counts,
            "referenced_history": referenced_history,
            "model_available": selection["model_available"],
            "status": "failed",
        })
        write_json(materials_file, [])
        return {
            "candidates": candidates,
            "rejected": pool_rejected,
            "rejection_counts": rejection_counts,
            "decisions": decisions,
            "referenced_history": referenced_history,
            "selections": [],
            "materials_files": [],
            "selection_file": str(selection_file),
            "materials_file": str(materials_file),
            "model_available": selection["model_available"],
            "status": "failed",
        }

    # Write master selection file with all picks
    write_json(selection_file, {
        "candidates": candidates,
        "selected": selections,
        "decisions": decisions,
        "rejection_counts": rejection_counts,
        "referenced_history": referenced_history,
        "model_available": selection["model_available"],
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
        "rejected": pool_rejected,
        "rejection_counts": rejection_counts,
        "decisions": decisions,
        "referenced_history": referenced_history,
        "selections": selections,
        "materials_files": materials_files,
        "selection_file": str(selection_file),
        "materials_file": str(materials_file),
        "model_available": selection["model_available"],
        "status": "success",
    }
