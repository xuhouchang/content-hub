"""Run/date inventory and the evidence-backed funnel.

M0 of the 2026-09-28 iteration requires that every available run can be traced
from collected material to a WeChat draft, and that missing recent history is
reported as ``unknown`` rather than inferred. This module only reads existing
artifacts (``platform/jobs``, ``platform/datasets``, ``platform/curate``,
``platform/ingest``, ``queue/`` and the article topic ledger). It never writes
pipeline state.

Failure classes the funnel can distinguish:

- ``material_shortage``  — no collected/curated material to select from.
- ``model_failure``      — the writer failed on an LLM/model error.
- ``quality_rejection``  — the final draft failed the quality gate (writer rc 2).
- ``enqueue_failure``    — the writer succeeded but no queue record exists.
- ``wechat_failure``     — the publish worker moved a record to ``failed.jsonl``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _read_records(path: Path) -> list[dict]:
    records: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    # verifykit-allow: no-unified-fallback-bypass read-only audit: missing/unreadable queue file yields no records
    except OSError:
        return records
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _stage(count: int | None, evidence: list[str], reason: str = "") -> dict:
    return {
        "count": count,
        "status": "known" if count is not None else "unknown",
        "evidence": evidence,
        "reason": reason,
    }


def _list_len(payload: Any, key: str | None = None) -> int | None:
    if payload is None:
        return None
    if key is not None:
        if isinstance(payload, dict):
            value = payload.get(key)
            return len(value) if isinstance(value, list) else None
        return None
    return len(payload) if isinstance(payload, list) else None


def _record_date(record: dict) -> str:
    """Best-effort YYYY-MM-DD associated with a queue/ledger record."""
    for key in ("enqueued_at", "finished_at", "date"):
        value = str(record.get(key, "") or "")
        if (
            len(value) >= 10
            and value[4] == "-"
            and value[7] == "-"
            and value[:4].isdigit()
        ):
            return value[:10]
    return ""


def _discover_dates(workspace_dir: Path) -> list[str]:
    """Every date with any evidence: jobs, datasets, queue records or ledgers.

    Dates that appear only in the publish queue (or only in a topic ledger) must
    still be emitted; their upstream stages are then reported as unknown rather
    than omitted or fabricated.
    """
    dates: set[str] = set()
    for root_key in ("jobs", "datasets"):
        root = workspace_dir / "platform" / root_key
        if root.is_dir():
            for child in root.iterdir():
                if child.is_dir() and len(child.name) == 10 and child.name[4] == "-":
                    dates.add(child.name)

    queue_dir = workspace_dir / "queue"
    for filename in ("pending.jsonl", "processing.jsonl", "done.jsonl", "failed.jsonl"):
        for record in _read_records(queue_dir / filename):
            found = _record_date(record)
            if found:
                dates.add(found)

    for ledger in ("_article_topics.json", "_case_topics.json", "_recent_topics.json"):
        payload = _read_json(workspace_dir / "wechat-articles" / ledger) or []
        if isinstance(payload, list):
            for entry in payload:
                if isinstance(entry, dict):
                    found = _record_date(entry)
                    if found:
                        dates.add(found)

    return sorted(dates, reverse=True)


def _job_status(jobs_dir: Path, job_name: str) -> tuple[str | None, dict]:
    job_file = jobs_dir / job_name / "job.json"
    if not job_file.exists():
        return None, {}
    job = _read_json(job_file) or {}
    return job.get("status"), job


def _queue_state(
    pending_count: int,
    processing_count: int,
    done_count: int,
    failed_count: int,
) -> str:
    """Queue outcome for a date, from real queue evidence.

    ``failed`` means the publish worker moved a record to ``failed.jsonl``.
    Pending/processing records are NOT failures — they are simply not yet
    published.
    """
    if failed_count:
        return "failed"
    if processing_count:
        return "processing"
    if pending_count:
        return "pending"
    if done_count:
        return "done"
    return ""


def _classify_article_failure(
    job: dict,
    collected: int | None,
    candidates: int | None,
    selected: int | None,
    enqueued: int | None,
    drafts_done: int | None,
    queue_state: str = "",
) -> str:
    """Return a stable failure class for a date, or '' when none is evident.

    ``wechat_failure`` is reported ONLY when a record actually landed in the
    failed-publish state. A pending/processing record with no draft is
    not-yet-published, and is surfaced via ``queue_state`` instead of being
    mislabeled as a failure.
    """
    if collected == 0 or (candidates == 0 and (collected is None or collected == 0)):
        return "material_shortage"
    if candidates == 0:
        return "material_shortage"

    writer_codes = (job.get("artifacts") or {}).get("writer_exit_codes")
    if isinstance(writer_codes, list):
        if any(code == 2 for code in writer_codes):
            return "quality_rejection"
        if any(code not in (0, 2) for code in writer_codes):
            # Any non-zero, non-quality writer exit is treated as a model/stage
            # failure. The writer logs are kept as evidence in the job file.
            return "model_failure"
    if selected and enqueued == 0:
        return "enqueue_failure"
    if queue_state == "failed":
        return "wechat_failure"
    return ""


def build_funnel(workspace_dir: Path, date_str: str | None = None) -> dict:
    """Build the collected -> draft-success funnel from existing artifacts."""
    workspace_dir = Path(workspace_dir)
    dates = [date_str] if date_str else _discover_dates(workspace_dir)
    queue_dir = workspace_dir / "queue"
    queue_records = {
        "pending": _read_records(queue_dir / "pending.jsonl"),
        "processing": _read_records(queue_dir / "processing.jsonl"),
        "done": _read_records(queue_dir / "done.jsonl"),
        "failed": _read_records(queue_dir / "failed.jsonl"),
    }
    article_topics = _read_json(workspace_dir / "wechat-articles" / "_article_topics.json") or []
    if not isinstance(article_topics, list):
        article_topics = []

    runs: list[dict] = []
    for current in dates:
        runs.append(
            _build_run(
                workspace_dir,
                current,
                queue_records,
                article_topics,
            )
        )
    return {"workspace_dir": str(workspace_dir), "runs": runs}


def _records_on(records: list[dict], date_str: str) -> list[dict]:
    return [record for record in records if _record_date(record) == date_str]


def _build_run(
    workspace_dir: Path,
    date_str: str,
    queue_records: dict[str, list[dict]],
    article_topics: list[dict],
) -> dict:
    jobs_dir = workspace_dir / "platform" / "jobs" / date_str
    collect_status, collect_job = _job_status(jobs_dir, "collect-daily")
    article_status, article_job = _job_status(jobs_dir, "article-daily")
    collect_artifacts = collect_job.get("artifacts", {}) if collect_job else {}

    datasets_dir = workspace_dir / "platform" / "datasets" / date_str
    curate_dir = workspace_dir / "platform" / "curate" / date_str
    ingest_dir = workspace_dir / "platform" / "ingest" / "raw" / date_str

    raw_file = ingest_dir / "materials.json"
    curated_file = curate_dir / "materials.json"
    article_pool_file = datasets_dir / "article_pool.json"
    selection_file = datasets_dir / "article_selection.json"

    collected = collect_artifacts.get("raw_material_count")
    collected_evidence = []
    if collect_job:
        collected_evidence.append(str(jobs_dir / "collect-daily" / "job.json"))
    if raw_file.exists():
        collected_evidence.append(str(raw_file))
    if collected is None:
        collected = _list_len(_read_json(raw_file))

    curated = collect_artifacts.get("curated_material_count")
    curated_evidence = []
    if collect_job:
        curated_evidence.append(str(jobs_dir / "collect-daily" / "job.json"))
    if curated_file.exists():
        curated_evidence.append(str(curated_file))
    if curated is None:
        curated = _list_len(_read_json(curated_file))

    candidates = collect_artifacts.get("article_candidate_count")
    candidate_evidence = []
    if collect_job:
        candidate_evidence.append(str(jobs_dir / "collect-daily" / "job.json"))
    if article_pool_file.exists():
        candidate_evidence.append(str(article_pool_file))
    if candidates is None:
        candidates = _list_len(_read_json(article_pool_file), "candidates")

    selection_payload = _read_json(selection_file)
    selected = _list_len(selection_payload, "selected")
    selected_evidence = [str(selection_file)] if selection_payload is not None else []

    qualified = sum(
        1
        for entry in article_topics
        if str(entry.get("date", "")) == date_str and entry.get("cluster_ids")
    )
    qualified_evidence = [
        str(workspace_dir / "wechat-articles" / "_article_topics.json")
    ] if article_topics else []

    pending_records = _records_on(queue_records["pending"], date_str)
    processing_records = _records_on(queue_records["processing"], date_str)
    done_records = _records_on(queue_records["done"], date_str)
    failed_records = _records_on(queue_records["failed"], date_str)
    enqueued = (
        len(pending_records)
        + len(processing_records)
        + len(done_records)
        + len(failed_records)
    )
    enqueued_evidence = [
        str(queue_dir_path)
        for queue_dir_path in (
            workspace_dir / "queue" / "pending.jsonl",
            workspace_dir / "queue" / "processing.jsonl",
            workspace_dir / "queue" / "done.jsonl",
            workspace_dir / "queue" / "failed.jsonl",
        )
        if queue_dir_path.exists()
    ]

    drafts_done = sum(1 for record in done_records if record.get("media_id"))
    drafts_done_evidence = (
        [str(workspace_dir / "queue" / "done.jsonl")] if done_records else []
    )
    queue_state = _queue_state(
        len(pending_records),
        len(processing_records),
        len(done_records),
        len(failed_records),
    )

    stages = {
        "collected": _stage(collected, collected_evidence, "collect-daily artifacts and raw materials.json"),
        "scored": _stage(curated, curated_evidence, "curated materials.json / collect artifacts"),
        "article_candidates": _stage(
            candidates, candidate_evidence, "article_pool.json / collect artifacts"
        ),
        "selected": _stage(selected, selected_evidence, "article_selection.json"),
        "qualified_finals": _stage(
            qualified if article_topics else None,
            qualified_evidence,
            "article topic ledger (empty ledger means 0 qualified finals)",
        ),
        "enqueued": _stage(enqueued, enqueued_evidence, "queue records for this date"),
        "drafts_done": _stage(drafts_done, drafts_done_evidence, "queue/done.jsonl media_id"),
    }

    failure_class = _classify_article_failure(
        article_job, collected, candidates, selected, enqueued, drafts_done, queue_state
    )

    return {
        "date": date_str,
        "collect_status": collect_status,
        "article_status": article_status,
        "job_ids": [
            job.get("job_id")
            for job in (collect_job, article_job)
            if job.get("job_id")
        ],
        "stages": stages,
        "failure_class": failure_class,
        "queue_state": queue_state,
        "evidence": {
            "jobs_dir": str(jobs_dir),
            "datasets_dir": str(datasets_dir),
        },
    }


def render_markdown(funnel: dict) -> str:
    lines = [f"# Pipeline funnel — {funnel['workspace_dir']}", ""]
    header = (
        "| date | collected | scored | candidates | selected | qualified | "
        "enqueued | drafts | queue | status | failure |"
    )
    lines.append(header)
    lines.append(
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |"
    )
    for run in funnel["runs"]:
        stages = run["stages"]
        cells = []
        for key in (
            "collected",
            "scored",
            "article_candidates",
            "selected",
            "qualified_finals",
            "enqueued",
            "drafts_done",
        ):
            stage = stages[key]
            cells.append("unknown" if stage["count"] is None else str(stage["count"]))
        lines.append(
            "| {date} | {cells} | {queue} | {status} | {failure} |".format(
                date=run["date"],
                cells=" | ".join(cells),
                queue=run.get("queue_state") or "-",
                status=run.get("article_status") or "unknown",
                failure=run.get("failure_class") or "-",
            )
        )
    lines.append("")
    lines.append("Evidence paths:")
    for run in funnel["runs"]:
        lines.append(f"- {run['date']} -> {run['evidence']['jobs_dir']}")
        for key, stage in run["stages"].items():
            if stage["count"] is None:
                lines.append(f"    - {key}: unknown ({stage['reason']})")
            elif stage["evidence"]:
                lines.append(f"    - {key}: {stage['evidence'][0]}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the content pipeline funnel")
    parser.add_argument("--workspace", default=".", help="Workspace root (default: cwd)")
    parser.add_argument("--date", default=None, help="Only this date (YYYY-MM-DD)")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")
    args = parser.parse_args(argv)

    funnel = build_funnel(Path(args.workspace).resolve(), date_str=args.date)
    if args.json:
        print(json.dumps(funnel, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(funnel), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
