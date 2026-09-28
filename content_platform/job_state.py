import datetime
import json
import secrets
from pathlib import Path
from typing import Any

from content_platform.models.job import make_job


def make_run_id(job_type: str, date_str: str, now: datetime.datetime | None = None) -> str:
    """Unique per-invocation run ID.

    Same-day reruns therefore write separate records instead of overwriting one
    another.
    """
    moment = now or datetime.datetime.now().astimezone()
    return f"{job_type}_{date_str}_{moment.strftime('%H%M%S')}_{secrets.token_hex(2)}"


class JobStateStore:
    def __init__(self, job_dir: Path, date_str: str | None = None):
        self.job_dir = Path(job_dir)
        self.job_file = self.job_dir / "job.json"
        self.runs_dir = self.job_dir / "runs"
        self.run_id: str | None = None
        # Keep the mixed legacy layout readable: ``job.json`` remains the latest
        # view, while ``runs/<run_id>.json`` keeps every invocation.
        self.date_str = date_str or (self.job_dir.parent.name if self.job_dir.parent else "")
        self.date_dir = self.job_dir.parent
        self.index_file = self.date_dir / "index.json"
        self.job_dir.mkdir(parents=True, exist_ok=True)

    def start_job(
        self, job_type: str, date_str: str, run_id: str | None = None
    ) -> dict[str, Any]:
        self.date_str = date_str
        self.run_id = run_id or make_run_id(job_type, date_str)
        job = make_job(
            job_id=self.run_id,
            job_type=job_type,
            date_str=date_str,
        )
        self.write_job(job)
        return job

    def write_job(self, job: dict[str, Any]) -> None:
        job.setdefault("run_id", job.get("job_id"))
        if job.get("status") != "running" and not job.get("ended_at"):
            job["ended_at"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        payload = json.dumps(job, ensure_ascii=False, indent=2)
        # Latest view (backward compatible) plus an immutable per-run record.
        self.job_file.write_text(payload, encoding="utf-8")
        run_file = self.runs_dir / str(job.get("run_id") or job.get("job_id")) / "job.json"
        run_file.parent.mkdir(parents=True, exist_ok=True)
        run_file.write_text(payload, encoding="utf-8")
        self._update_index(job, run_file)

    def _update_index(self, job: dict[str, Any], run_file: Path) -> None:
        """Maintain the date-level index of every run's terminal evidence."""
        run_id = str(job.get("run_id") or job.get("job_id") or "")
        entries: list[dict] = []
        if self.index_file.exists():
            try:
                loaded = json.loads(self.index_file.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    entries = loaded
            except (json.JSONDecodeError, OSError):
                entries = []
        entry = {
            "run_id": run_id,
            "job_type": job.get("job_type", ""),
            "date": job.get("date", self.date_str),
            "status": job.get("status", ""),
            "stage_reason": job.get("stage_reason", ""),
            "started_at": job.get("started_at"),
            "ended_at": job.get("ended_at"),
            "job_file": str(run_file),
        }
        for index, existing in enumerate(entries):
            if str(existing.get("run_id")) == run_id:
                entries[index] = entry
                break
        else:
            entries.append(entry)
        self.date_dir.mkdir(parents=True, exist_ok=True)
        self.index_file.write_text(
            json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def read_job(self) -> dict[str, Any]:
        return json.loads(self.job_file.read_text(encoding="utf-8"))

    @property
    def run_dir(self) -> Path:
        """Directory holding this run's isolated artifacts (logs, run job.json).

        Falls back to the shared job directory only before ``start_job`` has
        assigned a run ID, so a rerun can never overwrite another run's logs.
        """
        if self.run_id:
            return self.runs_dir / self.run_id
        return self.job_dir

    def finalize(self, job: dict[str, Any], status: str, stage_reason: str = "") -> dict[str, Any]:
        """Record a terminal state with reason, then persist and return the job."""
        job["status"] = status
        job["stage_reason"] = stage_reason
        if not job.get("ended_at"):
            job["ended_at"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        self.write_job(job)
        return job

    def first_incomplete_step(self) -> dict[str, Any] | None:
        for step in self.read_job().get("steps", []):
            if step.get("status") != "success":
                return step
        return None
