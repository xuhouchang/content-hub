"""M3 offline scenarios: independent run evidence, terminal state and alerts.

No network, no LLM, no live writes: everything runs under ``tmp_path``.
"""

import json
import subprocess
from pathlib import Path

import content_platform.runtime as runtime_module
import platform_cli
from content_platform.alerts import record_job_result
from content_platform.job_state import JobStateStore, make_run_id


def _evidence_material(title: str, cluster: str) -> dict:
    return {
        "title": title,
        "url": f"https://example.com/{cluster}",
        "canonical_url": f"https://example.com/{cluster}",
        "summary": "after deployment, automation deflected 38% of tickets",
        "content_text": "the team measured 21% cost savings after deployment",
        "editorial_fit_score": 0.9,
        "novelty_score": 0.7,
        "quality": {"content_chars": 2000},
        "dedup": {"cluster_id": cluster},
    }


def _job_dir(tmp_path: Path) -> Path:
    return tmp_path / "platform" / "jobs" / "2026-06-03" / "article-daily"


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_run_ids_are_unique():
    ids = {make_run_id("article-daily", "2026-06-03") for _ in range(5)}
    assert len(ids) == 5


def test_same_day_reruns_write_separate_records(tmp_path: Path):
    job_dir = _job_dir(tmp_path)

    first = JobStateStore(job_dir, date_str="2026-06-03")
    job1 = first.start_job("article-daily", "2026-06-03")
    first.finalize(job1, "success")

    second = JobStateStore(job_dir, date_str="2026-06-03")
    job2 = second.start_job("article-daily", "2026-06-03")
    second.finalize(job2, "failed", "boom")

    run_files = sorted((job_dir / "runs").glob("*/job.json"))
    assert len(run_files) == 2
    assert {path.parent.name for path in run_files} == {job1["run_id"], job2["run_id"]}
    assert job1["run_id"] != job2["run_id"]

    index = _read_json(job_dir.parent / "index.json")
    statuses = {entry["run_id"]: entry["status"] for entry in index}
    assert statuses[job1["run_id"]] == "success"
    assert statuses[job2["run_id"]] == "failed"

    # Latest-view job.json stays readable and reflects the newest run.
    latest = _read_json(job_dir / "job.json")
    assert latest["run_id"] == job2["run_id"]
    assert latest["status"] == "failed"
    assert latest["ended_at"]


def test_article_daily_writes_per_article_logs(tmp_path: Path, monkeypatch):
    outputs = ["first ok", "second ok"]

    def fake_run(cmd, capture_output, text, timeout, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=outputs.pop(0), stderr="err")

    monkeypatch.setattr(runtime_module.subprocess, "run", fake_run)

    job = runtime_module.run_article_daily(
        date_str="2026-06-03",
        workspace_dir=tmp_path,
        materials=[_evidence_material("A", "c1"), _evidence_material("B", "c2")],
    )

    assert job["status"] == "success"
    job_dir = _job_dir(tmp_path)
    run_dir = job_dir / "runs" / job["run_id"]
    assert "first ok" in (run_dir / "writer_1_stdout.log").read_text(encoding="utf-8")
    assert "second ok" in (run_dir / "writer_2_stdout.log").read_text(encoding="utf-8")
    index = _read_json(job_dir.parent / "index.json")
    assert index[-1]["run_id"] == job["run_id"]
    assert index[-1]["status"] == "success"


def test_two_same_day_runs_keep_separate_writer_logs(tmp_path: Path, monkeypatch):
    outputs = iter(["run one output", "run two output"])

    def fake_run(cmd, capture_output, text, timeout, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=next(outputs), stderr="")

    monkeypatch.setattr(runtime_module.subprocess, "run", fake_run)

    first = runtime_module.run_article_daily(
        date_str="2026-06-03",
        workspace_dir=tmp_path,
        materials=[_evidence_material("First", "c1")],
    )
    second = runtime_module.run_article_daily(
        date_str="2026-06-03",
        workspace_dir=tmp_path,
        materials=[_evidence_material("Second", "c2")],
    )

    assert first["run_id"] != second["run_id"]
    job_dir = _job_dir(tmp_path)
    first_log = job_dir / "runs" / first["run_id"] / "writer_1_stdout.log"
    second_log = job_dir / "runs" / second["run_id"] / "writer_1_stdout.log"
    assert first_log.read_text(encoding="utf-8") == "run one output"
    assert second_log.read_text(encoding="utf-8") == "run two output"


def test_mixed_success_failure_records_partial_and_health(tmp_path: Path, monkeypatch):
    codes = iter([0, 2])
    monkeypatch.setattr(
        runtime_module.subprocess,
        "run",
        lambda cmd, capture_output, text, timeout, **kw: subprocess.CompletedProcess(
            cmd, next(codes), stdout="ok", stderr=""
        ),
    )

    job = runtime_module.run_article_daily(
        date_str="2026-06-03",
        workspace_dir=tmp_path,
        materials=[_evidence_material("A", "c1"), _evidence_material("B", "c2")],
    )

    assert job["status"] == "partial"
    assert job["artifacts"]["qualified_count"] == 1
    assert job["artifacts"]["health"]["consecutive_failures"] == 1
    assert job["artifacts"]["rejection_counts"].get("quality_rejected") == 1


def test_process_exception_records_terminal_state_and_health(tmp_path: Path, monkeypatch):
    def _boom(**kwargs):
        raise RuntimeError("pipeline exploded")

    monkeypatch.setattr(runtime_module, "run_daily_article_pipeline", _boom)

    job = runtime_module.run_article_daily(
        date_str="2026-06-03",
        workspace_dir=tmp_path,
        materials=[_evidence_material("A", "c1")],
    )

    assert job["status"] == "failed"
    assert "pipeline exploded" in job["artifacts"]["stage_reason"]
    assert job["stage_reason"] == job["artifacts"]["stage_reason"]
    assert job["artifacts"]["health"]["run_id"] == job["run_id"]
    assert any(step.get("name") == "exception" for step in job["steps"])

    job_dir = _job_dir(tmp_path)
    latest = _read_json(job_dir / "job.json")
    assert latest["status"] == "failed"
    assert latest["ended_at"]
    index = _read_json(job_dir.parent / "index.json")
    assert index[-1]["status"] == "failed"
    assert index[-1]["stage_reason"] == job["artifacts"]["stage_reason"]


def test_cli_exit_code_follows_recorded_status(tmp_path: Path, monkeypatch):
    def _boom(**kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(runtime_module, "run_daily_article_pipeline", _boom)

    code = platform_cli.main(
        ["run", "article-daily", "--date", "2026-06-03", "--workspace-dir", str(tmp_path)]
    )

    assert code == 1
    latest = _read_json(_job_dir(tmp_path) / "job.json")
    assert latest["status"] == "failed"


def test_cli_exit_code_is_zero_only_for_success(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(platform_cli, "run_article_daily", lambda *a, **k: {"status": "partial"})
    assert (
        platform_cli.main(
            ["run", "article-daily", "--date", "2026-06-03", "--workspace-dir", str(tmp_path)]
        )
        == 1
    )

    monkeypatch.setattr(platform_cli, "run_article_daily", lambda *a, **k: {"status": "success"})
    assert (
        platform_cli.main(
            ["run", "article-daily", "--date", "2026-06-03", "--workspace-dir", str(tmp_path)]
        )
        == 0
    )


def _health(tmp_path: Path) -> dict:
    return _read_json(tmp_path / "platform" / "state" / "pipeline_health.json")


def test_alert_emitted_once_and_deduplicated_by_run_id(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PIPELINE_ALERT_AFTER_FAILURES", "2")

    first = record_job_result(tmp_path, "article-daily", "failed", run_id="run-1")
    second = record_job_result(tmp_path, "article-daily", "failed", run_id="run-2")
    duplicate = record_job_result(tmp_path, "article-daily", "failed", run_id="run-2")
    third = record_job_result(tmp_path, "article-daily", "failed", run_id="run-3")

    assert first["alert"] is False
    assert second["alert"] is True  # exactly one alert at the threshold
    assert duplicate["alert"] is False  # duplicate trigger for the same run
    assert duplicate["consecutive_failures"] == second["consecutive_failures"]
    assert third["alert"] is False  # alert already active

    state = _health(tmp_path)["article-daily"]
    assert state["alert_run_id"] == "run-2"
    assert state["alert_active"] is True


def test_replayed_older_run_id_is_idempotent(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PIPELINE_ALERT_AFTER_FAILURES", "2")

    first = record_job_result(tmp_path, "article-daily", "failed", run_id="run-1")
    second = record_job_result(tmp_path, "article-daily", "failed", run_id="run-2")
    # Replaying an older run ID out of order must be idempotent.
    replay = record_job_result(tmp_path, "article-daily", "failed", run_id="run-1")

    assert first["consecutive_failures"] == 1
    assert second["consecutive_failures"] == 2
    assert replay["consecutive_failures"] == 2
    assert replay["alert"] is False
    state = _health(tmp_path)["article-daily"]
    assert state["consecutive_failures"] == 2

    # A genuinely new failure still counts, and the active alert stays deduped.
    third = record_job_result(tmp_path, "article-daily", "failed", run_id="run-3")
    assert third["consecutive_failures"] == 3
    assert third["alert"] is False


def test_recovery_clears_alert_and_records_it(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PIPELINE_ALERT_AFTER_FAILURES", "2")

    record_job_result(tmp_path, "article-daily", "failed", run_id="run-1")
    record_job_result(tmp_path, "article-daily", "failed", run_id="run-2")
    recovered = record_job_result(tmp_path, "article-daily", "success", run_id="run-3")

    assert recovered["recovered"] is True
    state = _health(tmp_path)["article-daily"]
    assert state["alert_active"] is False
    assert state["consecutive_failures"] == 0
    assert state["recovered_at"]

    # A fresh failure streak starts over after recovery.
    again = record_job_result(tmp_path, "article-daily", "failed", run_id="run-4")
    assert again["alert"] is False
    assert again["consecutive_failures"] == 1
