"""M0 offline fixtures: evidence-backed funnel and failure classification.

These tests build synthetic workspaces under tmp_path. They must never touch the
live repo's queue/, wechat-articles/ or platform/state/.
"""

import json
from pathlib import Path

from content_platform.audit.funnel import build_funnel, render_markdown


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_records(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


def _article_job(workspace: Path, date: str, writer_codes: list[int]) -> None:
    _write_json(
        workspace / "platform" / "jobs" / date / "article-daily" / "job.json",
        {
            "job_id": f"article-daily_{date}_001",
            "job_type": "article-daily",
            "date": date,
            "status": "partial" if any(c != 0 for c in writer_codes) else "success",
            "steps": [],
            "artifacts": {"writer_exit_codes": writer_codes},
        },
    )


def test_funnel_reports_known_stages_with_evidence(tmp_path: Path):
    date = "2026-08-22"
    _write_json(
        tmp_path / "platform" / "jobs" / date / "collect-daily" / "job.json",
        {
            "job_id": f"collect-daily_{date}_001",
            "status": "success",
            "artifacts": {
                "raw_material_count": 40,
                "curated_material_count": 12,
                "article_candidate_count": 5,
            },
        },
    )
    _article_job(tmp_path, date, [0, 0])
    _write_json(
        tmp_path / "platform" / "datasets" / date / "article_selection.json",
        {"selected": [{"title": "a"}, {"title": "b"}], "status": "success"},
    )
    _write_json(
        tmp_path / "wechat-articles" / "_article_topics.json",
        [{"date": date, "title": "a", "cluster_ids": ["c1"], "source_urls": []}],
    )
    _write_records(
        tmp_path / "queue" / "pending.jsonl",
        [{"output_dir": "/tmp/a", "enqueued_at": f"{date}T06:10:00+08:00"}],
    )

    funnel = build_funnel(tmp_path, date_str=date)
    run = funnel["runs"][0]
    stages = run["stages"]

    assert stages["collected"]["count"] == 40
    assert stages["scored"]["count"] == 12
    assert stages["article_candidates"]["count"] == 5
    assert stages["selected"]["count"] == 2
    assert stages["qualified_finals"]["count"] == 1
    assert stages["enqueued"]["count"] == 1
    assert stages["drafts_done"]["count"] == 0
    assert run["failure_class"] == "wechat_failure"
    assert any("job.json" in path or "jobs" in path for path in stages["collected"]["evidence"])
    # Markdown renders without touching disk.
    assert "| date |" in render_markdown(funnel)


def test_funnel_marks_missing_history_unknown(tmp_path: Path):
    funnel = build_funnel(tmp_path, date_str="2026-01-01")
    run = funnel["runs"][0]
    for key in ("collected", "scored", "article_candidates", "selected", "qualified_finals"):
        assert run["stages"][key]["status"] == "unknown", key
        assert run["stages"][key]["count"] is None, key
    # The queue is always readable, so an empty queue is a known zero (not unknown).
    assert run["stages"]["enqueued"] == {
        "count": 0,
        "status": "known",
        "evidence": [],
        "reason": "queue records for this date",
    }
    assert run["failure_class"] == ""


def test_funnel_classifies_material_shortage(tmp_path: Path):
    date = "2026-08-23"
    _write_json(
        tmp_path / "platform" / "jobs" / date / "collect-daily" / "job.json",
        {"status": "success", "artifacts": {"raw_material_count": 0, "curated_material_count": 0}},
    )
    run = build_funnel(tmp_path, date_str=date)["runs"][0]
    assert run["failure_class"] == "material_shortage"


def test_funnel_classifies_quality_rejection(tmp_path: Path):
    date = "2026-08-24"
    _article_job(tmp_path, date, [2])
    _write_json(
        tmp_path / "platform" / "jobs" / date / "collect-daily" / "job.json",
        {"status": "success", "artifacts": {"raw_material_count": 30, "article_candidate_count": 3}},
    )
    run = build_funnel(tmp_path, date_str=date)["runs"][0]
    assert run["failure_class"] == "quality_rejection"


def test_funnel_discovers_dates_from_jobs_and_datasets(tmp_path: Path):
    _article_job(tmp_path, "2026-08-20", [0])
    _write_json(tmp_path / "platform" / "datasets" / "2026-08-21" / "article_pool.json", {"candidates": []})
    dates = [run["date"] for run in build_funnel(tmp_path)["runs"]]
    assert dates == ["2026-08-21", "2026-08-20"]
