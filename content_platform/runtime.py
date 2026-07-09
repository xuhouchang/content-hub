from datetime import datetime
from pathlib import Path
import subprocess
import sys

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
    
    newly_curated = []
    for material, raw in zip(clustered, new_materials):
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
    return subprocess.run(cmd, capture_output=True, text=True, timeout=600)


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

    # Write multiple articles (one per selected cluster)
    materials_files = result.get("materials_files", [result.get("materials_file")] if result.get("materials_file") else [])
    for i, mat_file in enumerate(materials_files):
        if mat_file and Path(mat_file).exists():
            print(f"\n── Article {i+1}/{len(materials_files)} from {mat_file} ──")
            legacy_result = _invoke_legacy_writer(
                resolved_workspace / "write_article.py",
                date_str=date_str,
                materials_file=mat_file,
                extra_args=["--publish"],
            )
            writer_all_stdouts.append(legacy_result.stdout or "")
            writer_all_exit_codes.append(legacy_result.returncode)
        else:
            writer_all_exit_codes.append(-1)

    job["artifacts"] = {
        "selection_file": result.get("selection_file"),
        "materials_file": result.get("materials_file"),
        "writer_exit_codes": writer_all_exit_codes,
    }

    # Capture artifacts from first writer run for backwards compat
    if materials_files and writer_all_stdouts:
        stdout_combined = "\n---\n".join(writer_all_stdouts)
        import tempfile
        tmp_stdout = job_dir / "writer_stdout.log"
        tmp_stdout.write_text(stdout_combined, encoding="utf-8")
        job["artifacts"]["writer_stdout_log"] = str(tmp_stdout)

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
