from datetime import datetime
from pathlib import Path
import os
import subprocess
import sys
import time

from content_platform.business.case_study.pipeline import run_case_study_pipeline
from content_platform.business.daily_article.pipeline import run_daily_article_pipeline
from content_platform.curate.clustering import assign_clusters
from content_platform.curate.scoring import editorial_fit_score
from content_platform.curate.scoring import execution_detail_score
from content_platform.cleanup import prune_old_platform_data
from content_platform.datasets.article_pool import build_article_pool
from content_platform.datasets.case_pool import build_case_pool
from content_platform.ingest.blogs import load_blog_materials
from content_platform.ingest.cases import load_case_materials
from content_platform.ingest.consulting import load_consulting_materials
from content_platform.ingest.rss import load_rss_materials
from content_platform.job_state import JobStateStore
from content_platform.normalize.canonicalize import build_material_record
from content_platform.paths import PlatformPaths
from content_platform.storage.json_store import read_json
from content_platform.storage.json_store import write_json

# Content-hash dedup registry (cross-run reprint detection). Guarded so the
# module still imports if lib's content_platform dependency is unavailable.
try:
    from lib import is_content_duplicate, mark_content_seen
except Exception:  # pragma: no cover
    is_content_duplicate = None
    mark_content_seen = None

# Only substantive bodies are trusted for content-level dedup; stubs would
# otherwise collide on a constant hash.
_MIN_CONTENT_DEDUP_CHARS = 200


def load_curated_cache(date_str: str, paths: PlatformPaths) -> dict[str, dict]:
    """Load previously scored materials, keyed by URL fingerprint.
    
    Loads from:
    1. Today's incremental cache (datasets/{date_str}/curated_cache.json)
    2. Global merged cache (datasets/curated_cache_all.json)
    
    Today's cache takes priority if same URL exists in both.
    """
    cache: dict[str, dict] = {}

    # 1. Global merged cache (cross-day accumulated)
    global_cache_file = paths.platform_dir / "datasets" / "curated_cache_all.json"
    if global_cache_file.exists():
        items = read_json(global_cache_file)
        if items and isinstance(items, list):
            for item in items:
                url = item.get("url", "") or item.get("canonical_url", "") or item.get("normalized_url", "")
                if url:
                    cache[url] = item

    # 2. Today's incremental cache (overrides global)
    cache_file = paths.datasets_dir(date_str) / "curated_cache.json"
    if cache_file.exists():
        items = read_json(cache_file)
        if items and isinstance(items, list):
            for item in items:
                url = item.get("url", "") or item.get("canonical_url", "") or item.get("fingerprint", "")
                if url:
                    cache[url] = item

    return cache


def _merge_into_global_cache(global_cache_file: Path, new_items: list[dict]) -> None:
    """Merge newly scored items into the global cross-day cache, keyed by URL."""
    existing = read_json(global_cache_file)
    if not existing or not isinstance(existing, list):
        existing = []
    seen = set()
    merged = []
    for item in existing:
        url = item.get("url", "") or item.get("canonical_url", "") or item.get("normalized_url", "")
        if url:
            seen.add(url)
            merged.append(item)
    additions = 0
    for item in new_items:
        url = item.get("url", "") or item.get("canonical_url", "") or item.get("normalized_url", "")
        if url and url not in seen:
            seen.add(url)
            merged.append(item)
            additions += 1
    if additions:
        write_json(global_cache_file, merged)
        print(f"  [cache] merged {additions} new items into global cache ({len(merged)} total)")


def _fingerprint(material: dict) -> str:
    """Generate a stable dedup key for a material."""
    return (material.get("url", "") or material.get("title", "")).strip()


def _curate_materials(
    raw_materials: list[dict],
    date_str: str,
    workspace_dir: Path | None = None,
) -> list[dict]:
    resolved_workspace = Path(workspace_dir or Path.cwd())
    paths = PlatformPaths.from_workspace(resolved_workspace)
    resolved = Path(workspace_dir or Path.cwd()) if workspace_dir else Path.cwd()
    paths = PlatformPaths.from_workspace(resolved)
    
    # Load existing curated cache to skip re-scoring
    cache = load_curated_cache(date_str, paths)
    
    # Split into new vs cached
    new_materials = []
    cached_items = []
    for m in raw_materials:
        fp = _fingerprint(m)
        if fp in cache:
            cached_items.append(cache[fp])
        else:
            new_materials.append(m)
    
    if not new_materials:
        return cached_items
    
    normalized = [
        build_material_record(material, collected_at=f"{date_str}T00:00:00+00:00")
        for material in new_materials
    ]
    clustered = assign_clusters(normalized)

    # Drop content-level duplicates. Two cases:
    #  • within-run: two URLs carrying the same body in this batch
    #  • cross-run: a reprint whose hash was persisted when it was previously
    #    committed to a writer (see run_article_daily / run_case_daily).
    # Note: we only *read* the registry here; persistence happens at commit
    # time so a fresh collect never flags its own just-collected material.
    # The registry is workspace-relative so it spans days within a workspace.
    seen_hashes: dict[str, str] = {}
    kept = []
    for c, r in zip(clustered, new_materials):
        h = c.get("content_hash", "")
        if h and len(c.get("content_text", "")) >= _MIN_CONTENT_DEDUP_CHARS:
            dup_of = seen_hashes.get(h)
            if dup_of is None and is_content_duplicate is not None:
                dup_of = is_content_duplicate(h, workspace_dir=resolved_workspace)
            if dup_of:
                print(f"  🧹 Content-dedup dropped reprint of {str(dup_of)[:60]}")
                continue
            seen_hashes[h] = c.get("canonical_url") or c.get("url", "")
        kept.append((c, r))

    newly_curated = []
    for material, raw in kept:
        enriched = dict(material)
        enriched["editorial_fit_score"] = editorial_fit_score(
            {
                "title": enriched.get("title", ""),
                "summary": enriched.get("summary", ""),
                "content_text": enriched.get("content_text", ""),
                "tags": enriched.get("tags", {}),
            }
        )
        if "execution_detail_score" in raw:
            enriched["execution_detail_score"] = raw["execution_detail_score"]
        else:
            enriched["execution_detail_score"] = execution_detail_score(
                {
                    "title": enriched.get("title", ""),
                    "summary": enriched.get("summary", ""),
                    "content_text": enriched.get("content_text", ""),
                }
            )
        if "novelty_score" in raw:
            enriched["novelty_score"] = raw["novelty_score"]
        if "url" in raw:
            enriched["url"] = raw["url"]
        if "content" in raw:
            enriched["content"] = raw["content"]
        elif "content_text" in raw:
            enriched["content"] = raw["content_text"]
        newly_curated.append(enriched)
    
    all_curated = cached_items + newly_curated
    return all_curated


def run_collect_daily(
    date_str: str,
    workspace_dir: Path | None = None,
    materials: list[dict] | None = None,
) -> dict:
    resolved_workspace = Path(workspace_dir or Path.cwd())
    paths = PlatformPaths.from_workspace(resolved_workspace)
    job_dir = paths.job_dir(date_str, "collect-daily")
    store = JobStateStore(job_dir)

    job = store.start_job(job_type="collect-daily", date_str=date_str)
    raw_materials = materials
    if raw_materials is None:
        raw_materials = []
        raw_materials.extend(load_rss_materials(date_str))
        raw_materials.extend(load_blog_materials(date_str))
        raw_materials.extend(load_consulting_materials(date_str))
        raw_materials.extend(load_case_materials(date_str))
    curated_materials = _curate_materials(raw_materials, date_str=date_str, workspace_dir=workspace_dir)
    article_pool = build_article_pool(curated_materials, topic_memory={"recent_outputs": []})
    case_pool = build_case_pool(curated_materials, topic_memory={"recent_outputs": []})

    write_json(paths.ingest_raw_dir(date_str) / "materials.json", raw_materials)
    write_json(paths.normalize_dir(date_str) / "materials.json", curated_materials)
    write_json(paths.curate_dir(date_str) / "materials.json", curated_materials)
    write_json(paths.datasets_dir(date_str) / "article_pool.json", article_pool)
    write_json(paths.datasets_dir(date_str) / "case_pool.json", case_pool)
    # Save curated cache for incremental scoring tomorrow
    write_json(paths.datasets_dir(date_str) / "curated_cache.json", curated_materials)
    
    # Also merge into the global cross-day cache
    global_cache_file = paths.platform_dir / "datasets" / "curated_cache_all.json"
    if global_cache_file.exists():
        _merge_into_global_cache(global_cache_file, curated_materials)

    job["steps"] = [
        {"name": "collect_sources", "status": "success"},
        {"name": "normalize_materials", "status": "success"},
        {"name": "curate_materials", "status": "success"},
        {"name": "build_datasets", "status": "success"},
    ]
    job["status"] = "success"
    job["artifacts"] = {
        "ingest_file": str(paths.ingest_raw_dir(date_str) / "materials.json"),
        "normalize_file": str(paths.normalize_dir(date_str) / "materials.json"),
        "curate_file": str(paths.curate_dir(date_str) / "materials.json"),
        "article_pool_file": str(paths.datasets_dir(date_str) / "article_pool.json"),
        "case_pool_file": str(paths.datasets_dir(date_str) / "case_pool.json"),
    }
    store.write_job(job)
    return job


def run_cleanup(date_str: str, workspace_dir: Path | None = None) -> dict:
    resolved_workspace = Path(workspace_dir or Path.cwd())
    paths = PlatformPaths.from_workspace(resolved_workspace)
    job_dir = paths.job_dir(date_str, "cleanup")
    store = JobStateStore(job_dir)

    job = store.start_job(job_type="cleanup", date_str=date_str)
    prune_old_platform_data(
        paths.platform_dir,
        today=datetime.strptime(date_str, "%Y-%m-%d").date(),
    )
    job["steps"] = [{"name": "cleanup_platform_data", "status": "success"}]
    job["status"] = "success"
    store.write_job(job)
    return job


def _load_dataset_candidates(
    paths: PlatformPaths,
    date_str: str,
    dataset_name: str,
) -> list[dict]:
    dataset_file = paths.datasets_dir(date_str) / dataset_name
    if not dataset_file.exists():
        return []
    payload = read_json(dataset_file)
    return payload.get("candidates", [])


def _invoke_legacy_writer(script_path: Path, date_str: str, materials_file: str, extra_args: list[str] | None = None) -> subprocess.CompletedProcess:
    cmd = [
        sys.executable,
        str(script_path),
        "--date",
        date_str,
        "--materials",
        materials_file,
    ]
    if extra_args:
        cmd.extend(extra_args)
    # Generous budget for the WRITE stage now that publishing is decoupled into
    # a separate process. 900s covers writing + polish + image search + embed,
    # while per-call LLM timeouts/retries are governed centrally in lib/llm.py.
    # P1-2: inject an absolute LLM deadline (epoch seconds) so call_model()
    # short-circuits the OpenRouter->DeepSeek fallback before the parent
    # timeout. 840s leaves ~60s headroom under the 900s parent budget.
    env = os.environ.copy()
    # R1: single global budget (840s leaves ~60s headroom under the 900s
    # parent). LLM_DEADLINE governs lib/llm.call_model; IMAGE_SEARCH_DEADLINE
    # governs the image-search stage (multi-image worst case can run hundreds
    # of seconds) so it can self-degrade to placeholders instead of blowing
    # the parent budget. Both are inherited by write_article.py and its child
    # image_search.py subprocesses.
    env["LLM_DEADLINE"] = str(time.time() + 840)
    env["IMAGE_SEARCH_DEADLINE"] = str(time.time() + 840)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=900, env=env)


def _write_job_log(job_dir: Path, filename: str, content: str) -> str:
    path = job_dir / filename
    path.write_text(content, encoding="utf-8")
    return str(path)


def _extract_output_dir(stdout: str) -> str | None:
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if "Output:" not in line:
            continue
        return line.split("Output:", 1)[1].strip().rstrip("/")
    return None


def _capture_writer_artifacts(job_dir: Path, result: subprocess.CompletedProcess) -> dict:
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    artifacts = {
        "writer_returncode": result.returncode,
        "writer_stdout_log": _write_job_log(job_dir, "writer_stdout.log", stdout),
        "writer_stderr_log": _write_job_log(job_dir, "writer_stderr.log", stderr),
    }
    output_dir = _extract_output_dir(stdout)
    if output_dir:
        artifacts["writer_output_dir"] = output_dir
    return artifacts


def _persist_selected_content_hashes(mat_file: str, workspace_dir=None):
    """Record the content hashes of materials committed to a writer.

    This is what makes cross-run content-dedup actually work: a reprint seen
    on a later day is dropped by `_curate_materials` because its hash is already
    here. Only substantive bodies are persisted (stubs are skipped).
    """
    if mark_content_seen is None:
        return
    try:
        data = read_json(Path(mat_file))
    except Exception:
        return
    items = data if isinstance(data, list) else data.get("materials", [])
    for m in items:
        h = m.get("content_hash", "")
        if h and len(m.get("content_text", "")) >= _MIN_CONTENT_DEDUP_CHARS:
            mark_content_seen(h, m.get("canonical_url") or m.get("url", ""), workspace_dir=workspace_dir)


def run_article_daily(
    date_str: str,
    workspace_dir: Path | None = None,
    materials: list[dict] | None = None,
) -> dict:
    resolved_workspace = Path(workspace_dir or Path.cwd())
    paths = PlatformPaths.from_workspace(resolved_workspace)
    job_dir = paths.job_dir(date_str, "article-daily")
    store = JobStateStore(job_dir)
    input_materials = materials or _load_dataset_candidates(paths, date_str, "article_pool.json")

    job = store.start_job(job_type="article-daily", date_str=date_str)
    result = run_daily_article_pipeline(
        date_str=date_str,
        workspace_dir=resolved_workspace,
        materials=input_materials,
        article_count=2,
    )
    job["steps"] = [
        {"name": "build_article_pool", "status": "success"},
        {"name": "select_primary_cluster", "status": "success"},
    ]
    job["status"] = result.get("status", "failed")

    writer_all_stdouts: list[str] = []
    writer_all_exit_codes: list[int] = []
    writer_artifacts: dict = {}

    # Write multiple articles (one per selected cluster)
    materials_files = result.get("materials_files", [result.get("materials_file")] if result.get("materials_file") else [])
    for i, mat_file in enumerate(materials_files):
        if mat_file and Path(mat_file).exists():
            print(f"\n── Article {i+1}/{len(materials_files)} from {mat_file} ──")
            legacy_result = _invoke_legacy_writer(
                resolved_workspace / "write_article.py",
                date_str=date_str,
                materials_file=mat_file,
                extra_args=None,
            )
            # Persist content hashes only on a successful write, so the registry
            # reflects *published* content. This is what lets a later-day reprint
            # (different URL, same body) be deduped — without ever flagging this
            # run's own freshly-collected material.
            if legacy_result.returncode == 0:
                _persist_selected_content_hashes(mat_file, workspace_dir=resolved_workspace)
            writer_all_stdouts.append(legacy_result.stdout or "")
            writer_all_exit_codes.append(legacy_result.returncode)
            # Capture writer logs + return code (mirrors run_case_daily).
            if i == 0:
                writer_artifacts = _capture_writer_artifacts(job_dir, legacy_result)
        else:
            writer_all_exit_codes.append(-1)

    job["artifacts"] = {
        "selection_file": result.get("selection_file"),
        "materials_file": result.get("materials_file"),
        "writer_exit_codes": writer_all_exit_codes,
    }
    job["artifacts"].update(writer_artifacts)

    all_success = all(c == 0 for c in writer_all_exit_codes)
    if not all_success:
        job["status"] = "partial" if len(materials_files) > 1 else "failed"
    store.write_job(job)
    return job


def run_case_daily(
    date_str: str,
    workspace_dir: Path | None = None,
    materials: list[dict] | None = None,
) -> dict:
    resolved_workspace = Path(workspace_dir or Path.cwd())
    paths = PlatformPaths.from_workspace(resolved_workspace)
    job_dir = paths.job_dir(date_str, "case-daily")
    store = JobStateStore(job_dir)
    input_materials = materials or _load_dataset_candidates(paths, date_str, "case_pool.json")

    job = store.start_job(job_type="case-daily", date_str=date_str)
    result = run_case_study_pipeline(
        date_str=date_str,
        workspace_dir=resolved_workspace,
        materials=input_materials,
    )
    job["steps"] = [
        {"name": "build_case_pool", "status": "success"},
        {"name": "rank_case_candidates", "status": "success"},
    ]
    job["status"] = result.get("status", "failed")
    job["artifacts"] = {
        "selection_file": result.get("selection_file"),
        "materials_file": result.get("materials_file"),
    }
    if job["status"] == "success" and result.get("materials_file"):
        # Persist content hashes so a later-day reprint is deduped.
        _persist_selected_content_hashes(result["materials_file"], workspace_dir=resolved_workspace)
        legacy_result = _invoke_legacy_writer(
            resolved_workspace / "decompose_case_study.py",
            date_str=date_str,
            materials_file=result["materials_file"],
        )
        job["artifacts"].update(_capture_writer_artifacts(job_dir, legacy_result))
        if legacy_result.returncode != 0:
            job["status"] = "failed"
    store.write_job(job)
    return job


def load_materials_file(materials_file: str | None) -> list[dict] | None:
    if not materials_file:
        return None
    path = Path(materials_file)
    if not path.exists():
        return None
    return read_json(path)
