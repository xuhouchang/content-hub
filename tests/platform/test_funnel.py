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
    # Pending is not-yet-published, not a WeChat failure.
    assert run["failure_class"] == ""
    assert run["queue_state"] == "pending"
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


def test_funnel_queue_only_date_is_discovered_with_unknown_upstream(tmp_path: Path):
    # Regression: 2026-08-23 exists only in the queue, not in jobs/datasets.
    date = "2026-08-23"
    _write_records(
        tmp_path / "queue" / "pending.jsonl",
        [
            {"output_dir": f"/tmp/q{i}", "enqueued_at": f"{date}T06:10:0{i}+08:00"}
            for i in range(3)
        ],
    )

    funnel = build_funnel(tmp_path)
    dates = [run["date"] for run in funnel["runs"]]
    assert date in dates, dates
    run = next(r for r in funnel["runs"] if r["date"] == date)

    for key in ("collected", "scored", "article_candidates", "selected", "qualified_finals"):
        assert run["stages"][key]["status"] == "unknown", key
        assert run["stages"][key]["count"] is None, key
    assert run["stages"]["enqueued"]["count"] == 3
    assert run["queue_state"] == "pending"
    assert run["failure_class"] == ""


def _queue_only_date(tmp_path: Path, state: str, records: list[dict]) -> dict:
    _write_records(tmp_path / "queue" / f"{state}.jsonl", records)
    return build_funnel(tmp_path, date_str="2026-08-24")["runs"][0]


def test_funnel_queue_state_pending_only(tmp_path: Path):
    run = _queue_only_date(
        tmp_path, "pending", [{"output_dir": "/tmp/p", "enqueued_at": "2026-08-24T06:00:00+08:00"}]
    )
    assert run["queue_state"] == "pending"
    assert run["failure_class"] == ""


def test_funnel_queue_state_processing_only(tmp_path: Path):
    run = _queue_only_date(
        tmp_path, "processing", [{"output_dir": "/tmp/p", "enqueued_at": "2026-08-24T06:00:00+08:00"}]
    )
    assert run["queue_state"] == "processing"
    assert run["failure_class"] == ""


def test_funnel_queue_state_done(tmp_path: Path):
    run = _queue_only_date(
        tmp_path,
        "done",
        [{"output_dir": "/tmp/d", "enqueued_at": "2026-08-24T06:00:00+08:00", "media_id": "M1"}],
    )
    assert run["queue_state"] == "done"
    assert run["failure_class"] == ""
    assert run["stages"]["drafts_done"]["count"] == 1


def test_funnel_queue_state_failed_is_wechat_failure(tmp_path: Path):
    run = _queue_only_date(
        tmp_path,
        "failed",
        [{"output_dir": "/tmp/f", "enqueued_at": "2026-08-24T06:00:00+08:00", "attempts": 3, "error": "errcode 40164"}],
    )
    assert run["queue_state"] == "failed"
    assert run["failure_class"] == "wechat_failure"


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
