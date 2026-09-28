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
) -> dict:
    """Persist failure streaks and emit a one-time local alert at the threshold."""
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
            streak = previous_streak + 1 if failed else 0
            alert_active = bool(current.get("alert_active", False)) if failed else False
            should_alert = failed and streak >= threshold and not alert_active
            current.update({
                "status": status,
                "last_run_at": now,
                "consecutive_failures": streak,
                "last_error": (reason or status)[:500] if failed else "",
                "alert_active": alert_active or should_alert,
            })
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
        "recovered": not failed and previous_streak >= threshold,
        "state_file": str(state_file),
    }
    if should_alert:
        print(
            f"🚨 ALERT {job_name}: {streak} consecutive failed runs "
            f"(threshold={threshold}); {reason or status}. State: {state_file}"
        )
    elif event["recovered"]:
        print(f"✅ RECOVERED {job_name}: next run succeeded after {previous_streak} failures")
    return event
