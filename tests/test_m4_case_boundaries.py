"""M4 offline scenarios: case/article boundaries and explicit workspace paths.

No network, no LLM, no live writes: everything runs under ``tmp_path``.
"""

import datetime
import json
import subprocess
from pathlib import Path

import content_platform.runtime as runtime_module
from content_platform.business.case_study.pipeline import run_case_study_pipeline
from content_platform.business.daily_article.topic_planner import select_candidates
from content_platform.datasets.article_pool import build_article_pool
from content_platform.datasets.case_pool import build_case_pool
from content_platform.recent_topics import append_topic


def _today() -> str:
    return datetime.datetime.now().astimezone().date().isoformat()


def _material(cluster: str, *, url: str | None = None, execution: float = 0.8) -> dict:
    resolved_url = url or f"https://example.com/{cluster}"
    return {
        "title": f"Case {cluster}",
        "url": resolved_url,
        "canonical_url": resolved_url,
        "summary": "after deployment, automation deflected 38% of tickets",
        "content_text": "the team measured 21% cost savings after deployment",
        "editorial_fit_score": 0.9,
        "execution_detail_score": execution,
        "novelty_score": 0.7,
        "quality": {"content_chars": 2000},
        "dedup": {"cluster_id": cluster},
    }


def _write_topic(workspace: Path, kind: str, cluster: str, url: str) -> None:
    append_topic(workspace, kind, {
        "title": f"{kind} {cluster}",
        "digest": "d",
        "date": _today(),
        "output_dir": "",
        "source_urls": [url],
        "cluster_ids": [cluster],
    })


def test_case_diversity_is_checked_only_against_case_history(tmp_path: Path):
    # An ARTICLE cluster does not block the same cluster in the case pool.
    _write_topic(tmp_path, "article", "cluster-1", "https://example.com/art1")
    material = _material("cluster-1")
    pool = build_case_pool([material], {"recent_outputs": []}, workspace_dir=tmp_path)
    assert [m["dedup"]["cluster_id"] for m in pool["candidates"]] == ["cluster-1"]

    # Recorded in CASE history, it is excluded from the case pool.
    _write_topic(tmp_path, "case", "cluster-1", "https://example.com/case1")
    pool_after = build_case_pool([material], {"recent_outputs": []}, workspace_dir=tmp_path)
    assert pool_after["candidates"] == []


def test_article_diversity_is_not_affected_by_case_history(tmp_path: Path):
    _write_topic(tmp_path, "case", "cluster-9", "https://example.com/case9")
    material = _material("cluster-9")
    pool = build_article_pool([material], {"recent_outputs": []}, workspace_dir=tmp_path)
    assert [m["dedup"]["cluster_id"] for m in pool["candidates"]] == ["cluster-9"]


def test_cross_type_source_url_dedup_skips_case_candidate(tmp_path: Path):
    # A URL already used by an ARTICLE is skipped by the CASE pipeline.
    _write_topic(tmp_path, "article", "cluster-art", "https://example.com/shared")
    result = run_case_study_pipeline(
        date_str="2026-06-03",
        workspace_dir=tmp_path,
        materials=[_material("case-1", url="https://example.com/shared/")],
    )
    assert result["selected"] is None
    assert result["status"] == "failed"


def test_cross_type_source_url_dedup_skips_article_candidate(tmp_path: Path):
    # A URL already used by a CASE is skipped by the article planner.
    _write_topic(tmp_path, "case", "cluster-case", "https://example.com/shared")
    result = select_candidates(
        [_material("art-1", url="https://example.com/shared")], n=1, workspace_dir=tmp_path
    )
    assert result["selected"] == []
    assert result["decisions"][0]["reason"] == "recent_source_url"


def test_two_workspaces_do_not_share_history(tmp_path: Path):
    workspace_a = tmp_path / "a"
    workspace_b = tmp_path / "b"
    _write_topic(workspace_a, "case", "cluster-x", "https://example.com/x")
    _write_topic(workspace_a, "article", "cluster-y", "https://example.com/y")

    case_material = _material("cluster-x")
    assert build_case_pool([case_material], {"recent_outputs": []}, workspace_dir=workspace_a)["candidates"] == []
    assert [
        m["dedup"]["cluster_id"]
        for m in build_case_pool([case_material], {"recent_outputs": []}, workspace_dir=workspace_b)["candidates"]
    ] == ["cluster-x"]

    article_material = _material("cluster-y")
    assert build_article_pool([article_material], {"recent_outputs": []}, workspace_dir=workspace_a)["candidates"] == []
    assert build_article_pool([article_material], {"recent_outputs": []}, workspace_dir=workspace_b)["candidates"]


def test_cross_type_url_dedup_uses_tracking_aware_normalization(tmp_path: Path):
    # History stored a tracking-param + fragment variant of the URL.
    _write_topic(
        tmp_path,
        "article",
        "cluster-art",
        "https://example.com/post/?utm_source=newsletter#top",
    )
    # The clean equivalent must be skipped by the case pipeline.
    result = run_case_study_pipeline(
        date_str="2026-06-03",
        workspace_dir=tmp_path,
        materials=[_material("case-clean", url="https://example.com/post")],
    )
    assert result["selected"] is None
    assert result["status"] == "failed"

    # Reverse direction: a case URL with a query variant blocks the clean
    # article candidate.
    _write_topic(tmp_path, "case", "cluster-case", "https://example.com/report?utm_medium=email")
    article = select_candidates(
        [_material("art-clean", url="https://example.com/report")],
        n=1,
        workspace_dir=tmp_path,
    )
    assert article["selected"] == []
    assert article["decisions"][0]["reason"] == "recent_source_url"


def test_content_hash_dedup_at_article_and_case_boundaries(tmp_path: Path):
    from lib import mark_content_seen

    body = "enterprise deployment rollout detail " * 20  # > MIN_CONTENT_DEDUP_CHARS
    mark_content_seen("sha256:deadbeef", "https://example.com/original", workspace_dir=tmp_path)

    article = _material("art-reprint", url="https://example.com/art-reprint")
    article["content_hash"] = "sha256:deadbeef"
    article["content_text"] = body
    article_pool = build_article_pool(
        [article], {"recent_outputs": []}, workspace_dir=tmp_path
    )
    assert article_pool["candidates"] == []
    assert any(r["reason"] == "recent_content_hash" for r in article_pool["rejected"])

    case = _material("case-reprint", url="https://example.com/case-reprint")
    case["content_hash"] = "sha256:deadbeef"
    case["content_text"] = body
    case_pool = build_case_pool([case], {"recent_outputs": []}, workspace_dir=tmp_path)
    assert case_pool["candidates"] == []
    assert any(r["reason"] == "recent_content_hash" for r in case_pool["rejected"])


def test_content_hash_dedup_applies_to_explicit_material_inputs(tmp_path: Path, monkeypatch):
    from lib import mark_content_seen

    body = "enterprise deployment rollout detail " * 20
    mark_content_seen("sha256:feedface", "https://example.com/case-original", workspace_dir=tmp_path)
    monkeypatch.setattr(
        runtime_module.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0] if a else "cmd", 0, stdout="ok", stderr=""),
    )

    case_material = _material("case-reprint", url="https://example.com/case-reprint")
    case_material["content_hash"] = "sha256:feedface"
    case_material["content_text"] = body
    case_job = runtime_module.run_case_daily(
        date_str="2026-06-03", workspace_dir=tmp_path, materials=[case_material]
    )
    assert case_job["status"] == "failed"
    assert case_job["artifacts"]["selected_count"] == 0

    article_material = _material("art-reprint", url="https://example.com/art-reprint")
    article_material["content_hash"] = "sha256:feedface"
    article_material["content_text"] = body
    article_job = runtime_module.run_article_daily(
        date_str="2026-06-03", workspace_dir=tmp_path, materials=[article_material]
    )
    assert article_job["status"] == "failed"
    assert article_job["artifacts"]["candidate_count"] == 0
    assert article_job["artifacts"]["rejection_counts"].get("recent_content_hash") == 1


def test_record_case_recent_topic_writes_comparable_cluster_ids(tmp_path: Path):
    materials_file = tmp_path / "case_materials.json"
    materials_file.write_text(json.dumps([_material("cluster-7")]), encoding="utf-8")

    runtime_module.record_case_recent_topic(str(materials_file), workspace_dir=tmp_path, out_dir="out")

    ledger = json.loads(
        (tmp_path / "wechat-articles" / "_case_topics.json").read_text(encoding="utf-8")
    )
    assert ledger[0]["cluster_ids"] == ["cluster-7"]
    assert ledger[0]["source_urls"] == ["https://example.com/cluster-7"]


def test_case_daily_increment_uses_case_history(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        runtime_module.subprocess,
        "run",
        lambda cmd, capture_output, text, timeout, **kw: subprocess.CompletedProcess(
            cmd, 0, stdout="ok", stderr=""
        ),
    )

    first = runtime_module.run_case_daily(
        date_str="2026-06-03",
        workspace_dir=tmp_path,
        materials=[_material("cluster-inc", url="https://example.com/inc1")],
    )
    assert first["status"] == "success"
    ledger = json.loads(
        (tmp_path / "wechat-articles" / "_case_topics.json").read_text(encoding="utf-8")
    )
    assert ledger[0]["cluster_ids"] == ["cluster-inc"]

    # Same case cluster, different URL: excluded by case-only diversity.
    second = runtime_module.run_case_daily(
        date_str="2026-06-04",
        workspace_dir=tmp_path,
        materials=[_material("cluster-inc", url="https://example.com/inc2")],
    )
    assert second["status"] == "failed"
