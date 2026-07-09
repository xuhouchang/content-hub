#!/usr/bin/env python3
"""Consume the publish queue and create WeChat drafts (async publish stage).

This is Stage B of the decoupled pipeline. Writing (write_article.py) only
produces the article + enqueues it here; this worker runs as a SEPARATE
process and creates the WeChat DRAFT (no auto group-send — human review gate).

Design notes:
- Reads queue/pending.jsonl, publishes each, then moves the record to
  queue/done.jsonl (with media_id) or queue/failed.jsonl (with error).
- Idempotent: a record already in done/failed is never republished.
- A file lock serializes concurrent workers (background trigger + cron net).
- wechat_publish.py is invoked WITHOUT a tight parent timeout and WITHOUT
  --publish, so it manages its own per-image timeouts and only drafts.

Usage:
  python3 publish_worker.py --once      # process queue and exit
  python3 publish_worker.py --daemon    # loop forever (service mode)
"""
import argparse
import datetime
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
QUEUE_DIR = SCRIPT_DIR / "queue"
PENDING = QUEUE_DIR / "pending.jsonl"
DONE = QUEUE_DIR / "done.jsonl"
FAILED = QUEUE_DIR / "failed.jsonl"
LOCK = QUEUE_DIR / ".worker.lock"
WECHAT_PUBLISH = SCRIPT_DIR / "wechat_publish.py"

# Generous parent timeout: wechat_publish.py handles its own per-image
# timeouts + fault tolerance now, so we only guard against a total hang.
PARENT_TIMEOUT = 900


def _ts() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _load_pending() -> list:
    if not PENDING.exists():
        return []
    records = []
    for line in PENDING.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _done_set() -> set:
    s = set()
    for f in (DONE, FAILED):
        if f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    s.add(json.loads(line).get("output_dir"))
                except json.JSONDecodeError:
                    continue
    return s


def _publish_one(rec: dict):
    output_dir = Path(rec["output_dir"])
    article = output_dir / rec.get("article", "article.md")
    images_dir = output_dir / rec.get("images_dir", "images")
    digest = rec.get("digest", "")
    if not article.exists():
        raise FileNotFoundError(f"article not found: {article}")
    cmd = [sys.executable, str(WECHAT_PUBLISH),
           "--article", str(article), "--images-dir", str(images_dir)]
    if digest:
        cmd += ["--digest", digest]
    # No --publish => create DRAFT only (human review gate).
    # No tight timeout => let the hardened wechat_publish.py manage per-image
    # timeouts and skip failed images instead of aborting the whole publish.
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=PARENT_TIMEOUT)
    out = result.stdout or ""
    if result.returncode != 0:
        err = (result.stderr or "")[-500:]
        raise RuntimeError(f"wechat_publish exited {result.returncode}: {err}")
    media_id = ""
    for line in out.splitlines():
        if "Draft created" in line:
            media_id = line.split(":", 1)[-1].strip()
            break
    return media_id, out


def _move(rec: dict, target: Path, extra: dict):
    target.parent.mkdir(parents=True, exist_ok=True)
    rec.update(extra)
    with target.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _rebuild_pending(records: list):
    done = _done_set()
    kept = [r for r in records if r.get("output_dir") not in done]
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    with PENDING.open("w", encoding="utf-8") as f:
        for r in kept:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _acquire_lock():
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(LOCK), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def process_once() -> int:
    fd = _acquire_lock()
    if fd is None:
        print(f"[{_ts()}] ⚠️ Another publish worker is running; skipping this run")
        return 0
    try:
        records = _load_pending()
        if not records:
            print(f"[{_ts()}] Queue empty. Nothing to publish.")
            return 0
        processed = 0
        for rec in records:
            od = rec.get("output_dir", "")
            if od in _done_set():
                continue  # safety: never double-publish
            try:
                media_id, _ = _publish_one(rec)
                _move(rec, DONE, {"published": False, "media_id": media_id,
                                  "finished_at": _ts()})
                print(f"[{_ts()}] ✅ Draft created: {od} (media_id={media_id[:12]}...)")
                processed += 1
            except Exception as e:
                _move(rec, FAILED, {"error": str(e)[:500], "finished_at": _ts()})
                print(f"[{_ts()}] ❌ Failed: {od}: {e}")
        _rebuild_pending(records)
        return processed
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser(description="Publish worker (Stage B)")
    parser.add_argument("--once", action="store_true", help="Process queue once and exit")
    parser.add_argument("--daemon", action="store_true", help="Loop forever")
    parser.add_argument("--interval", type=int, default=300, help="Daemon poll interval (s)")
    args = parser.parse_args()

    if args.daemon:
        print(f"[{_ts()}] Publish worker daemon started (interval={args.interval}s)")
        while True:
            try:
                process_once()
            except Exception as e:
                print(f"[{_ts()}] worker error: {e}")
            time.sleep(args.interval)
    else:
        n = process_once()
        print(f"[{_ts()}] Publish worker done. Processed {n} article(s).")


if __name__ == "__main__":
    main()
