"""Local consecutive-failure alerts for scheduled content jobs."""

from __future__ import annotations

import datetime
import fcntl
import json
import os
from pathlib import Path


def record_job_result(
    workspace_dir: Path,
    job_name: str,
    status: str,
    reason: str = "",
    run_id: str = "",
) -> dict:
    """Persist failure streaks and emit a one-time local alert at the threshold.

    Alerts are deduplicated by run ID: a repeated trigger for the same failing
    run never re-alerts and does not inflate the streak. Exactly one alert is
    emitted when the streak first reaches the threshold; recovery clears the
    active alert and is recorded, so the next failure starts a fresh streak.

    External delivery (Feishu/mail) stays pending until a human supplies a
    recipient and credential source; this function only records local evidence
    and prints the alert.
    """
    try:
        threshold = max(1, int(os.environ.get("PIPELINE_ALERT_AFTER_FAILURES", "2")))
    except ValueError:
        threshold = 2

    state_file = Path(workspace_dir) / "platform" / "state" / "pipeline_health.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_file.with_suffix(".lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            try:
                payload = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
            except (json.JSONDecodeError, OSError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}

            now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
            current = payload.get(job_name, {})
            if not isinstance(current, dict):
                current = {}

            failed = status not in {"success", "skipped"}
            try:
                previous_streak = int(current.get("consecutive_failures", 0) or 0)
            except (TypeError, ValueError):
                previous_streak = 0

            processed_run_ids = [
                str(existing)
                for existing in (current.get("processed_run_ids") or [])
                if existing
            ]
            # Deduplicate against every run already recorded for this job, not
            # just the immediately previous one, so replaying an older run ID
            # (out of order) is still idempotent.
            is_replay = bool(run_id) and run_id in processed_run_ids

            if is_replay:
                # A replayed run never changes the streak, never re-alerts and
                # never re-records recovery.
                streak = previous_streak
                alert_active = bool(current.get("alert_active", False))
                should_alert = False
                recovered = False
            else:
                if failed:
                    streak = previous_streak + 1
                else:
                    streak = 0
                alert_active = bool(current.get("alert_active", False)) if failed else False
                already_alerted_for_run = bool(run_id) and run_id == str(
                    current.get("alert_run_id", "") or ""
                )
                should_alert = (
                    failed
                    and streak >= threshold
                    and not alert_active
                    and not already_alerted_for_run
                )
                recovered = not failed and previous_streak >= threshold
                current.update({
                    "status": status,
                    "last_run_at": now,
                    "last_run_id": run_id or current.get("last_run_id", ""),
                    "consecutive_failures": streak,
                    "last_error": (reason or status)[:500] if failed else "",
                    "alert_active": alert_active or should_alert,
                })
                if should_alert:
                    current["alert_run_id"] = run_id or current.get("last_run_id", "")
                    current["last_alert_at"] = now
                if recovered:
                    current["recovered_at"] = now
                    current["alert_active"] = False
                if run_id:
                    processed_run_ids.append(run_id)
            if processed_run_ids:
                # Bounded so the state file cannot grow without limit.
                current["processed_run_ids"] = processed_run_ids[-50:]
            payload[job_name] = current
            temp = state_file.with_suffix(".json.tmp")
            temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(state_file)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    event = {
        "consecutive_failures": streak,
        "threshold": threshold,
        "alert": should_alert,
        "recovered": recovered,
        "run_id": run_id,
        "state_file": str(state_file),
    }
    if should_alert:
        print(
            f"🚨 ALERT {job_name}: {streak} consecutive failed runs "
            f"(threshold={threshold}); run_id={run_id or 'n/a'}; "
            f"{reason or status}. State: {state_file}"
        )
    elif recovered:
        print(
            f"✅ RECOVERED {job_name}: next run succeeded after {previous_streak} failures "
            f"(run_id={run_id or 'n/a'})"
        )
    return event
