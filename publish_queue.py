#!/usr/bin/env python3
"""Shared, lock-protected publish queue operations."""

import datetime
import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
QUEUE_DIR = SCRIPT_DIR / "queue"
PENDING = QUEUE_DIR / "pending.jsonl"
PROCESSING = QUEUE_DIR / "processing.jsonl"
DONE = QUEUE_DIR / "done.jsonl"
FAILED = QUEUE_DIR / "failed.jsonl"
QUEUE_LOCK = QUEUE_DIR / ".queue.lock"


@contextmanager
def queue_lock() -> Iterator[None]:
    """Serialize writers and the worker while queue files are mutated."""
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(QUEUE_LOCK), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def read_records_unlocked(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
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


def write_records_unlocked(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
    os.replace(temp, path)


def append_record_unlocked(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False) + "\n")


def enqueue_for_publish(output_dir: str, digest: str = "") -> bool:
    """Add one article idempotently without racing a running worker."""
    normalized_dir = str(Path(output_dir).resolve())
    with queue_lock():
        for path in (PENDING, PROCESSING, DONE, FAILED):
            if any(
                str(Path(record.get("output_dir", "")).resolve()) == normalized_dir
                for record in read_records_unlocked(path)
                if record.get("output_dir")
            ):
                print(f"  ↺ Already queued/resolved ({path.name}); skip enqueue")
                return False

        append_record_unlocked(
            PENDING,
            {
                "output_dir": normalized_dir,
                "article": "article.md",
                "images_dir": "images",
                "digest": digest,
                "attempts": 0,
                "enqueued_at": datetime.datetime.now()
                .astimezone()
                .isoformat(timespec="seconds"),
            },
        )
    return True
