from pathlib import Path
import subprocess

import content_platform.runtime as runtime_module
from content_platform.runtime import run_article_daily, run_case_daily, run_collect_daily
from content_platform.storage.json_store import read_json


LONG_ARTICLE_TEXT = " ".join(
    [
        "enterprise workflow redesign manager incentives trust governance adoption resistance permissions"
        for _ in range(30)
    ]
)


def test_collect_daily_writes_job_file(tmp_path: Path):
    result = run_collect_daily(
        date_str="2026-06-03",
        workspace_dir=tmp_path,
        materials=[
            {
                "url": "https://example.com/adoption",
                "title": "Why enterprise AI adoption stalls",
                "summary": "workflow redesign and manager incentives; 38% faster after deployment",
                "content_text": LONG_ARTICLE_TEXT,
                "source_type": "rss",
                "source_name": "Example Feed",
                "editorial_fit_score": 0.85,
                "quality": {"content_chars": len(LONG_ARTICLE_TEXT)},
                "execution_detail_score": 0.8,
                "novelty_score": 0.7,
            },
            {
                "url": "https://example.com/pricing",
                "title": "GitHub changes Copilot pricing tiers",
                "summary": "new package pricing for teams",
                "content_text": "pricing package subscription tier changes for paid plans",
                "source_type": "blogs",
                "source_name": "Example Blog",
                "execution_detail_score": 0.1,
                "novelty_score": 0.9,
            },
        ],
    )

    assert result["job_type"] == "collect-daily"
    assert (
        tmp_path / "platform" / "jobs" / "2026-06-03" / "collect-daily" / "job.json"
    ).exists()
    assert (
        tmp_path / "platform" / "ingest" / "raw" / "2026-06-03" / "materials.json"
    ).exists()
    assert (
        tmp_path / "platform" / "normalize" / "2026-06-03" / "materials.json"
    ).exists()
    assert (
        tmp_path / "platform" / "curate" / "2026-06-03" / "materials.json"
    ).exists()
    assert (
        tmp_path / "platform" / "datasets" / "2026-06-03" / "article_pool.json"
    ).exists()
    assert (
        tmp_path / "platform" / "datasets" / "2026-06-03" / "case_pool.json"
    ).exists()


def test_article_daily_writes_selection_artifact(tmp_path: Path):
    result = run_article_daily(
        date_str="2026-06-03",
        workspace_dir=tmp_path,
        materials=[
            {
                "title": "GitHub pricing tiers",
                "editorial_fit_score": 0.2,
                "novelty_score": 0.9,
                "dedup": {"cluster_id": "c1"},
                "quality": {"content_chars": 5000},
            },
            {
                "title": "Workflow redesign in support ops",
                "summary": "after deployment, automation deflected 38% of tickets",
                "editorial_fit_score": 0.9,
                "novelty_score": 0.7,
                "dedup": {"cluster_id": "c2"},
                "quality": {"content_chars": 5000},
                "url": "https://example.com/article",
                "content": "substantive content",
            },
        ],
    )

    assert result["job_type"] == "article-daily"
    assert (
        tmp_path / "platform" / "datasets" / "2026-06-03" / "article_selection.json"
    ).exists()
    assert (
        tmp_path / "platform" / "datasets" / "2026-06-03" / "article_materials.json"
    ).exists()


def test_article_daily_fails_when_no_candidates(tmp_path: Path):
    result = run_article_daily(
        date_str="2026-06-03",
        workspace_dir=tmp_path,
        materials=[
            {
                "title": "GitHub pricing tiers",
                "editorial_fit_score": 0.2,
                "novelty_score": 0.9,
                "dedup": {"cluster_id": "c1"},
                "quality": {"content_chars": 5000},
            },
        ],
    )

    assert result["status"] == "failed"


def test_case_daily_writes_selection_artifact(tmp_path: Path):
    result = run_case_daily(
        date_str="2026-06-03",
        workspace_dir=tmp_path,
        materials=[
            {
                "title": "Packaging update",
                "editorial_fit_score": 0.8,
                "execution_detail_score": 0.2,
                "dedup": {"cluster_id": "c1"},
            },
            {
                "title": "Support org agent deployment",
                "editorial_fit_score": 0.8,
                "execution_detail_score": 0.9,
                "dedup": {"cluster_id": "c2"},
                "url": "https://example.com/case",
                "content": "substantive content",
            },
        ],
    )

    assert result["job_type"] == "case-daily"
    assert (
        tmp_path / "platform" / "datasets" / "2026-06-03" / "case_selection.json"
    ).exists()
    assert (
        tmp_path / "platform" / "datasets" / "2026-06-03" / "case_materials.json"
    ).exists()


def test_case_daily_fails_when_no_candidates(tmp_path: Path):
    result = run_case_daily(
        date_str="2026-06-03",
        workspace_dir=tmp_path,
        materials=[
            {
                "title": "Packaging update",
                "editorial_fit_score": 0.8,
                "execution_detail_score": 0.2,
                "dedup": {"cluster_id": "c1"},
            },
        ],
    )

    assert result["status"] == "failed"


def test_article_daily_uses_collect_dataset_when_materials_not_provided(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        runtime_module.subprocess,
        "run",
        lambda cmd, capture_output, text, timeout, **kwargs: subprocess.CompletedProcess(
            cmd, 0, stdout="ok", stderr=""
        ),
    )
    run_collect_daily(
        date_str="2026-06-03",
        workspace_dir=tmp_path,
        materials=[
            {
                "url": "https://example.com/adoption",
                "title": "Why enterprise AI adoption stalls",
                "summary": "workflow redesign and manager incentives; 38% faster after deployment",
                "content_text": LONG_ARTICLE_TEXT,
                "source_type": "rss",
                "source_name": "Example Feed",
                "editorial_fit_score": 0.85,
                "quality": {"content_chars": len(LONG_ARTICLE_TEXT)},
                "execution_detail_score": 0.8,
                "novelty_score": 0.7,
            }
        ],
    )

    result = run_article_daily(date_str="2026-06-03", workspace_dir=tmp_path)

    # One candidate -> one qualified final draft -> partial under M2 semantics.
    assert result["status"] == "partial"
    assert result["artifacts"]["qualified_count"] == 1


def test_case_daily_uses_collect_dataset_when_materials_not_provided(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        runtime_module.subprocess,
        "run",
        lambda cmd, capture_output, text, timeout, **kwargs: subprocess.CompletedProcess(
            cmd, 0, stdout="ok", stderr=""
        ),
    )
    run_collect_daily(
        date_str="2026-06-03",
        workspace_dir=tmp_path,
        materials=[
            {
                "url": "https://example.com/case",
                "title": "Support org agent deployment",
                "summary": "execution details for workflow redesign",
                "content_text": LONG_ARTICLE_TEXT,
                "source_type": "rss",
                "source_name": "Example Feed",
                "editorial_fit_score": 0.9,
                "quality": {"content_chars": len(LONG_ARTICLE_TEXT)},
                "execution_detail_score": 0.9,
                "novelty_score": 0.7,
            }
        ],
    )

    result = run_case_daily(date_str="2026-06-03", workspace_dir=tmp_path)

    assert result["status"] == "success"


def test_article_daily_invokes_legacy_writer_with_materials_file(tmp_path: Path, monkeypatch):
    calls = []

    def fake_run(cmd, capture_output, text, timeout, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(runtime_module.subprocess, "run", fake_run)

    result = run_article_daily(
        date_str="2026-06-03",
        workspace_dir=tmp_path,
        materials=[
            {
                "title": "Workflow redesign in support ops",
                "summary": "after deployment, automation deflected 38% of tickets",
                "editorial_fit_score": 0.9,
                "novelty_score": 0.7,
                "dedup": {"cluster_id": "c2"},
                "quality": {"content_chars": 5000},
                "url": "https://example.com/article",
                "content": LONG_ARTICLE_TEXT,
            },
        ],
    )

    assert result["status"] == "partial"  # single article -> partial (M2)
    assert any("write_article.py" in part for part in calls[0])
    assert "--materials" in calls[0]


def test_case_daily_invokes_legacy_writer_with_materials_file(tmp_path: Path, monkeypatch):
    calls = []

    def fake_run(cmd, capture_output, text, timeout, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(runtime_module.subprocess, "run", fake_run)

    result = run_case_daily(
        date_str="2026-06-03",
        workspace_dir=tmp_path,
        materials=[
            {
                "title": "Support org agent deployment",
                "editorial_fit_score": 0.8,
                "execution_detail_score": 0.9,
                "dedup": {"cluster_id": "c2"},
                "url": "https://example.com/case",
                "content": LONG_ARTICLE_TEXT,
            },
        ],
    )

    assert result["status"] == "success"
    assert any("decompose_case_study.py" in part for part in calls[0])
    assert "--materials" in calls[0]


def test_collect_daily_uses_source_collectors_when_materials_not_provided(tmp_path: Path, monkeypatch):
    calls = []

    def fake_rss(date_str):
        calls.append(("rss", date_str))
        return [
            {
                "url": "https://example.com/rss",
                "title": "Enterprise workflow redesign",
                "summary": "workflow redesign adoption governance",
                "content_text": LONG_ARTICLE_TEXT,
                "source_type": "rss",
                "source_name": "RSS Feed",
                "execution_detail_score": 0.8,
                "novelty_score": 0.7,
            }
        ]

    def fake_reddit(date_str):
        calls.append(("reddit", date_str))
        return []

    def fake_blogs(date_str):
        calls.append(("blogs", date_str))
        return [
            {
                "url": "https://example.com/blog",
                "title": "Manager incentives for AI adoption",
                "summary": "enterprise adoption workflow manager incentives",
                "content_text": LONG_ARTICLE_TEXT,
                "source_type": "blogs",
                "source_name": "Blog Source",
                "execution_detail_score": 0.6,
                "novelty_score": 0.8,
            }
        ]

    def fake_consulting(date_str):
        calls.append(("consulting", date_str))
        return []

    def fake_cases(date_str):
        calls.append(("cases", date_str))
        return []

    monkeypatch.setattr(runtime_module, "load_rss_materials", fake_rss)
    monkeypatch.setattr(runtime_module, "load_reddit_materials", fake_reddit)
    monkeypatch.setattr(runtime_module, "load_blog_materials", fake_blogs)
    monkeypatch.setattr(runtime_module, "load_consulting_materials", fake_consulting)
    monkeypatch.setattr(runtime_module, "load_case_materials", fake_cases)

    result = run_collect_daily(date_str="2026-06-03", workspace_dir=tmp_path)
    raw_materials = read_json(
        tmp_path / "platform" / "ingest" / "raw" / "2026-06-03" / "materials.json"
    )

    assert result["status"] == "success"
    assert calls == [
        ("rss", "2026-06-03"),
        ("reddit", "2026-06-03"),
        ("blogs", "2026-06-03"),
        ("consulting", "2026-06-03"),
        ("cases", "2026-06-03"),
    ]
    assert [item["url"] for item in raw_materials] == [
        "https://example.com/rss",
        "https://example.com/blog",
    ]


def test_collect_daily_derives_execution_detail_score_when_not_provided(tmp_path: Path):
    result = run_collect_daily(
        date_str="2026-06-03",
        workspace_dir=tmp_path,
        materials=[
            {
                "url": "https://example.com/policy",
                "title": "Responsible Scaling Policy",
                "summary": "updated risk governance framework for frontier AI systems",
                "content_text": (
                    "this update introduces capability thresholds, internal governance, "
                    "risk management, safeguards, model evaluation processes, and "
                    "practical implementation guidance."
                ),
                "source_type": "blogs",
                "source_name": "Example Blog",
            }
        ],
    )

    curated = read_json(
        tmp_path / "platform" / "curate" / "2026-06-03" / "materials.json"
    )

    assert result["status"] == "success"
    # 纯治理/风险/安全素材（无企业落地证据）执行细节分必须 LOW（< CASE_DETAIL_THRESHOLD=0.6），
    # 不得仅凭 governance/framework/evaluation 等词成为案例池（case_pool）高分捷径。
    # 若执行细节分未提供，按新 rubric 由 governance cap + 缺 impl 证据推导为低值。
    assert curated[0]["execution_detail_score"] < 0.6


def test_article_daily_persists_writer_logs_and_return_code(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        runtime_module.subprocess,
        "run",
        lambda cmd, capture_output, text, timeout, **kwargs: subprocess.CompletedProcess(
            cmd,
            0,
            stdout="writer ok",
            stderr="warning line",
        ),
    )

    result = run_article_daily(
        date_str="2026-06-03",
        workspace_dir=tmp_path,
        materials=[
            {
                "title": "Workflow redesign in support ops",
                "summary": "after deployment, automation deflected 38% of tickets",
                "editorial_fit_score": 0.9,
                "novelty_score": 0.7,
                "dedup": {"cluster_id": "c2"},
                "quality": {"content_chars": 5000},
                "url": "https://example.com/article",
                "content": LONG_ARTICLE_TEXT,
            },
        ],
    )

    job_dir = tmp_path / "platform" / "jobs" / "2026-06-03" / "article-daily"
    assert result["status"] == "partial"  # single article -> partial (M2)
    assert result["artifacts"]["writer_returncode"] == 0
    assert Path(result["artifacts"]["writer_stdout_log"]).read_text(encoding="utf-8") == "writer ok"
    assert Path(result["artifacts"]["writer_stderr_log"]).read_text(encoding="utf-8") == "warning line"
    assert (job_dir / "writer_1_stdout.log").exists()
    assert (job_dir / "writer_1_stderr.log").exists()


def test_case_daily_fails_when_writer_returns_non_zero_and_persists_logs(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        runtime_module.subprocess,
        "run",
        lambda cmd, capture_output, text, timeout, **kwargs: subprocess.CompletedProcess(
            cmd,
            2,
            stdout="partial output",
            stderr="writer failed",
        ),
    )

    result = run_case_daily(
        date_str="2026-06-03",
        workspace_dir=tmp_path,
        materials=[
            {
                "title": "Support org agent deployment",
                "editorial_fit_score": 0.8,
                "execution_detail_score": 0.9,
                "dedup": {"cluster_id": "c2"},
                "url": "https://example.com/case",
                "content": LONG_ARTICLE_TEXT,
            },
        ],
    )

    assert result["status"] == "failed"
    assert result["artifacts"]["writer_returncode"] == 2
    assert Path(result["artifacts"]["writer_stdout_log"]).read_text(encoding="utf-8") == "partial output"
    assert Path(result["artifacts"]["writer_stderr_log"]).read_text(encoding="utf-8") == "writer failed"
