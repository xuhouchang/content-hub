from datetime import datetime
from pathlib import Path
import json
import os
import subprocess
import sys
import time

from content_platform.business.case_study.pipeline import run_case_study_pipeline
from content_platform.business.daily_article.pipeline import run_daily_article_pipeline
from content_platform.curate.clustering import assign_clusters
from content_platform.curate.scoring import editorial_fit_score, execution_detail_score, score_materials_batch
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

# ── LLM tagging integration ──
# Reuses tag_schema definitions from the original tag_materials.py
_TAG_FILE = Path(__file__).resolve().parent.parent / "wechat-articles" / "_material_tags.json"
_TAG_BATCH_SIZE = 10
_MIN_TAG_CONTENT_CHARS = 300

# Active tag dimensions (matches tag_schema.py)
_TAG_DIMENSIONS = [
    "content_form", "topic_focus", "perspective",
    "evidence_source", "evidence_depth", "tone", "entity_scope",
]


def _load_tag_db() -> dict:
    """Load existing LLM tag database (url -> tags)."""
    if _TAG_FILE.exists():
        try:
            return json.loads(_TAG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, Exception):
            return {}
    return {}


def _save_tag_db(tags: dict) -> None:
    """Persist the tag database."""
    _TAG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TAG_FILE.write_text(json.dumps(tags, ensure_ascii=False, indent=2), encoding="utf-8")


def _build_tag_definitions() -> str:
    """Build compact dimension definitions for the LLM prompt."""
    try:
        sys.path.insert(0, str(_TAG_FILE.parent.parent / "collector"))
        from tag_schema import TAG_SCHEMA
    except Exception:
        # Inline fallback if tag_schema not available
        return "(tag definitions unavailable)"
    lines = []
    for dim in _TAG_DIMENSIONS:
        info = TAG_SCHEMA.get(dim, {})
        desc = info.get("description", dim).replace("[已弃用] ", "")
        lines.append(f"## {desc} (key: {dim})")
        for tag in info.get("tags", []):
            lines.append(f"- {tag}")
        lines.append("")
    return "\n".join(lines)


_TAG_SYSTEM_PROMPT = """你是一个专业的内容标签系统。对每一篇文章从7个维度打标，每个维度选择最匹配的一个标签。

## 维度定义

1. content_form —— 内容的形式：研究报告（有数据方法）、深度分析（有观点链）、新闻（纯事实）、产品公告、高管访谈/对话（一对一的CEO/CIO采访）、案例复盘、周报（多篇汇总）等。

2. topic_focus —— 核心主题：Agent、LLM、企业AI落地、治理安全等。

3. perspective —— 叙事视角：从谁的角度写的？高管、工程师、员工、学术研究者、行业从业者、供应商？

4. evidence_source —— 论据来源：核心证据的性质。关键区分：一手数据/实验 vs 引用学术论文 vs 引用行业报告 vs 逻辑推演（无原始数据）。

5. evidence_depth —— 论证的深度和粒度：有数据+因果分析？有数据趋势描述？有案例+细节？还是纯观点/无支撑？

6. tone —— 基调：分析洞察、预警风险、实用指导、批判质疑、新闻式报道（被动传递信息）、分析式报道（报道+自己的判断）。

7. entity_scope —— 涉及范围：科技巨头、传统企业、创业公司、学术机构、政府、跨行业（理论/宏观）、跨行业（实践/标杆）。

## 关键区分

- "新闻式报道"：陈述事实——"OpenAI发布了o3，评测显示比o1提升20%"
- "分析式报道"：同样写一件事但给出判断——"OpenAI发布的o3暴露了一个信号：推理成本还在涨"
- "跨行业(理论/宏观)"：讨论AI对"企业管理"的通用影响，不举具体公司例子
- "跨行业(实践/标杆)"：讨论AI如何在多行业落地，但给出了具体企业案例

只输出JSON数组，不要额外内容。
"""


def _llm_tag_material(url: str, title: str, content_text: str) -> dict | None:
    """Call LLM to tag a single material. Returns tags dict or None on failure."""
    if len(content_text.strip()) < _MIN_TAG_CONTENT_CHARS:
        print(f"  [tag] Skipping (too short): {title[:40]}")
        return None

    tag_defs = _build_tag_definitions()
    dim_keys_str = ", ".join(_TAG_DIMENSIONS)

    content_preview = content_text[:3000]
    user_prompt = f"""Below is a material to tag. Read the content and assign tags from all {len(_TAG_DIMENSIONS)} dimensions.

Tag schema:
{tag_defs}

URL: {url}
Title: {title}
Content:
{content_preview}
---end---

Output a JSON array with 1 entry. Each entry must have:
- "url": the original URL
- "title": the original title
- "tags": an object with the following keys: {dim_keys_str}
- "key_signal": 1-2 sentence summary of the most surprising/noteworthy finding (empty string if none)

IMPORTANT: Only output the JSON array, nothing else."""

    messages = [
        {"role": "system", "content": _TAG_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    try:
        from lib.llm import call_model
        response = call_model(messages, temperature=0.3, max_tokens=2048, model="deepseek-v4-flash")
        if not response:
            return None
        start = response.find("[")
        end = response.rfind("]")
        if start >= 0 and end > start:
            results = json.loads(response[start:end+1])
            if isinstance(results, list) and len(results) > 0:
                return results[0]
    except Exception as e:
        print(f"  [tag] LLM call failed for '{title[:30]}': {e}")
    return None


def _enrich_tags(material: dict, raw: dict, tag_db: dict) -> dict:
    """Enrich a single normalized material with LLM tags.
    
    Looks up the tag database first; falls back to LLM call for new materials.
    Returns the enriched material (tags populated).
    """
    url = material.get("canonical_url", "") or material.get("url", "")
    if not url:
        return material

    url_key = url.rstrip("/")
    existing = tag_db.get(url_key)

    if existing and "tags" in existing:
        material["tags"] = existing["tags"]
        if existing.get("key_signal"):
            material["key_signal"] = existing["key_signal"]
        return material

    # New material — call LLM
    title = material.get("title", "")
    content_text = material.get("content_text", "")
    result = _llm_tag_material(url_key, title, content_text)

    if result and "tags" in result:
        # Normalize: topic_focus is a single string from LLM, but clustering expects a list
        tags = dict(result["tags"])
        if isinstance(tags.get("topic_focus"), str):
            tags["topic_focus"] = [tags["topic_focus"]]
        material["tags"] = tags
        if result.get("key_signal"):
            material["key_signal"] = result["key_signal"]
        # Persist to tag db
        tag_db[url_key] = {
            "title": title,
            "tags": tags,
            "key_signal": result.get("key_signal", ""),
            "tagged_at": datetime.now().isoformat(),
        }
    else:
        # LLM failed — leave tags empty (will be retried next run)
        print(f"  [tag] Failed to tag: {title[:40]}")

    return material

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

    # ── LLM tagging: enrich normalized materials before clustering ──
    # Tags improve cluster seed quality significantly over keyword-only fallback.
    print(f"\n  ✨ LLM tagging {len(normalized)} materials...")
    tag_db = _load_tag_db()
    tagged = []
    for m in normalized:
        tagged.append(_enrich_tags(m, {}, tag_db))
    _save_tag_db(tag_db)
    print(f"  ✨ Tagging complete ({len(tag_db)} total in database)")

    clustered = assign_clusters(tagged)

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
        enriched["editorial_fit_score"] = 0.0  # placeholder; filled by score_materials_batch
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

    # Run LLM batch scoring on all curated materials
    print(f"\n🧠 Running LLM batch scoring on {len(all_curated)} materials...")
    all_curated = score_materials_batch(all_curated)

    # Report score distribution
    scores = [m.get("editorial_fit_score", 0.0) for m in all_curated]
    if scores:
        above_065 = sum(1 for s in scores if s >= 0.65)
        print(f"  Score distribution: min={min(scores):.2f} max={max(scores):.2f} avg={sum(scores)/len(scores):.2f}")
        print(f"  Above 0.65 threshold: {above_065}/{len(scores)} materials")
        top5 = sorted(all_curated, key=lambda m: m.get("editorial_fit_score", 0.0), reverse=True)[:5]
        print(f"  Top 5 materials:")
        for t in top5:
            print(f"    • {t.get('editorial_fit_score', 0.0):.2f} — {t.get('title', '')[:60]}")
            reasoning = t.get("llm_score_reasoning", "")
            if reasoning:
                print(f"      🧠 {reasoning}")

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
        else:
            # Close the case-daily dedup loop: record the committed source URLs
            # so the next day's selection skips them (same registry the daily
            # article pipeline writes to). Fixes OlmoEarth-style daily reprints.
            record_case_recent_topic(
                result["materials_file"],
                workspace_dir=resolved_workspace,
                out_dir=job["artifacts"].get("writer_output_dir"),
            )
    store.write_job(job)
    return job


def load_materials_file(materials_file: str | None) -> list[dict] | None:
    if not materials_file:
        return None
    path = Path(materials_file)
    if not path.exists():
        return None
    return read_json(path)


def record_case_recent_topic(
    materials_file: str | None,
    workspace_dir: Path,
    out_dir: str | None = None,
) -> None:
    """Record a committed case study's source URLs into _recent_topics.json.

    This closes the dedup loop for case-daily: the daily-article pipeline
    already logs source URLs there (see write_article._log_article_topic), and
    the case-study selection now reads it to skip reprints. Without this,
    case studies never log their sources, so the same source URL keeps getting
    re-selected day after day (e.g. the Ai2 OlmoEarth weather-monitoring case
    that was published daily for days).
    """
    try:
        data = read_json(Path(materials_file)) if materials_file else None
    except Exception:
        data = None
    items = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("materials", []) or []
    if not items:
        return
    source_urls = []
    title = ""
    digest = ""
    for m in items:
        u = m.get("canonical_url") or m.get("url") or m.get("normalized_url", "")
        if u:
            source_urls.append(u)
        if not title:
            title = m.get("title", "") or ""
        if not digest:
            digest = m.get("summary", "") or m.get("digest", "") or ""
    if not source_urls:
        return
    recent_file = workspace_dir / "wechat-articles" / "_recent_topics.json"
    recent_file.parent.mkdir(parents=True, exist_ok=True)
    entries = []
    if recent_file.exists():
        try:
            entries = read_json(recent_file)
        except Exception:
            entries = []
    if not isinstance(entries, list):
        entries = []
    from datetime import date as _date
    entries.append({
        "title": title,
        "digest": digest,
        "date": _date.today().isoformat(),
        "output_dir": out_dir or str(Path(materials_file).parent if materials_file else ""),
        "source_urls": source_urls[:5],
        "cluster_ids": [],
    })
    entries = entries[-30:]
    write_json(recent_file, entries)
    print(f"  📌 Case recent-topic recorded: {source_urls[0][:60]}")


def _record_case_dedup_guard(workspace_dir: Path) -> None:
    """No-op placeholder retained for clarity; dedup reads recent_topics directly."""
    return
