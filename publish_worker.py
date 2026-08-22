#!/usr/bin/env python3
"""Consume the publish queue and create WeChat drafts (async publish stage).

This is Stage B of the decoupled pipeline. Writing (write_article.py) only
produces the article + enqueues it here; this worker runs as a SEPARATE
process and creates the WeChat DRAFT (no auto group-send — human review gate).

Design notes:
- Reads queue/pending.jsonl, publishes each, then moves the record to
  queue/done.jsonl (with media_id). Failures retry with backoff before the
  terminal queue/failed.jsonl state.
- Idempotent: a record already in done/failed is never republished.
- Separate worker and queue locks serialize consumers and producer mutations.
- wechat_publish.py is invoked WITHOUT a tight parent timeout and WITHOUT
  --publish, so it manages its own per-image timeouts and only drafts.

Usage:
  python3 publish_worker.py --once              # process queue and exit
  python3 publish_worker.py --once --quiet-empty # cron mode
  python3 publish_worker.py --daemon            # service mode
"""
import argparse
import datetime
import fcntl
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from publish_queue import (
    DONE,
    FAILED,
    PENDING,
    PROCESSING,
    QUEUE_DIR,
    append_record_unlocked,
    queue_lock,
    read_records_unlocked,
    write_records_unlocked,
)

SCRIPT_DIR = Path(__file__).resolve().parent
LOCK = QUEUE_DIR / ".worker.lock"
WECHAT_PUBLISH = SCRIPT_DIR / "wechat_publish.py"

# Generous parent timeout: wechat_publish.py handles its own per-image
# timeouts + fault tolerance now, so we only guard against a total hang.
PARENT_TIMEOUT = 900
MAX_ATTEMPTS = 3
BASE_RETRY_SECONDS = 300


def _ts() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _load_pending() -> list:
    with queue_lock():
        return read_records_unlocked(PENDING)


def _done_set() -> set:
    with queue_lock():
        return {
            record.get("output_dir")
            for path in (DONE, FAILED)
            for record in read_records_unlocked(path)
            if record.get("output_dir")
        }


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
    # P1-1: parse the structured marker wechat_publish.py prints, instead
    # of loosely matching "Draft created" (which also appears in create_draft()
    # with the title, polluting the media_id value).
    media_id = ""
    m = re.search(r"DRAFT_MEDIA_ID=(\S+)", out)
    if m:
        media_id = m.group(1)
    if not media_id:
        raise RuntimeError("wechat_publish succeeded without DRAFT_MEDIA_ID marker")
    return media_id, out


def _move(rec: dict, target: Path, extra: dict):
    moved = dict(rec)
    moved.update(extra)
    with queue_lock():
        append_record_unlocked(target, moved)


def _load_processing() -> list:
    with queue_lock():
        return read_records_unlocked(PROCESSING)


def _rebuild_pending_excluding(output_dir: str):
    """Rewrite pending.jsonl dropping a single output_dir (atomic-ish).

    P2-5: used to pull a record out of pending *before* publishing it, so a
    crash between draft creation and marking-done cannot re-enqueue the same
    article and create a duplicate WeChat draft.
    """
    with queue_lock():
        kept = [
            record
            for record in read_records_unlocked(PENDING)
            if record.get("output_dir") != output_dir
        ]
        write_records_unlocked(PENDING, kept)


def _mark_processing(rec: dict):
    """Move a record from pending into the in-progress file."""
    output_dir = rec.get("output_dir", "")
    with queue_lock():
        kept = [
            record
            for record in read_records_unlocked(PENDING)
            if record.get("output_dir") != output_dir
        ]
        write_records_unlocked(PENDING, kept)
        append_record_unlocked(PROCESSING, rec)


def _unmark_processing(rec: dict):
    """Remove a record from the in-progress file after it is resolved."""
    od = rec.get("output_dir", "")
    with queue_lock():
        recs = [
            record
            for record in read_records_unlocked(PROCESSING)
            if record.get("output_dir") != od
        ]
        write_records_unlocked(PROCESSING, recs)


def _patch_processing_media_id(output_dir: str, media_id: str):
    """Rewrite the in-progress record for output_dir to carry its media_id.

    R2: lets a crash between draft creation and _move() to done be self-healed
    (reuse the media_id) instead of leaving a stuck/duplicate entry.
    """
    with queue_lock():
        recs = read_records_unlocked(PROCESSING)
        changed = False
        for record in recs:
            if record.get("output_dir") == output_dir:
                record["media_id"] = media_id
                changed = True
        if changed:
            write_records_unlocked(PROCESSING, recs)


def _check_processing_leftovers():
    """Self-heal records a previous crashed run left mid-publish (R2 / P2-5).

    The live worker holds the flock, so any entry in queue/processing.jsonl is
    stale. We only auto-heal entries whose file is >15min old (extra guard
    against acting on a record written by a run we just took over from).

    - record already carries a media_id → draft was created but the run died
      before marking done → reuse that media_id, move straight to done.jsonl
      (no duplicate draft).
    - record has no media_id → died before create_draft → move back to
      pending.jsonl for a single safe retry (flock prevents concurrent
      double-send).
    """
    recs = _load_processing()
    if not recs:
        return
    try:
        age = time.time() - PROCESSING.stat().st_mtime
    except FileNotFoundError:
        return
    if age < 15 * 60:
        print(f"[{_ts()}] ℹ️ {len(recs)} record(s) in queue/processing.jsonl "
              f"still fresh (<15min); leaving for the active run to finish.")
        return
    healed = 0
    for rec in recs:
        od = rec.get("output_dir", "")
        if od in _done_set():
            healed += 1
            continue  # already resolved elsewhere; skip
        media_id = rec.get("media_id", "")
        if media_id:
            _move(rec, DONE, {"published": False, "media_id": media_id,
                              "finished_at": _ts(), "recovered": True})
            print(f"[{_ts()}] 🩹 Recovered mid-publish draft "
                  f"(reused media_id={media_id[:12]}...): {od}")
        else:
            _move(rec, PENDING, {})
            print(f"[{_ts()}] 🩹 Re-queued for single retry: {od}")
        healed += 1
    if healed:
        # Drain the stale in-progress file now that everything is re-homed.
        QUEUE_DIR.mkdir(parents=True, exist_ok=True)
        with queue_lock():
            write_records_unlocked(PROCESSING, [])
        print(f"[{_ts()}] 🩹 Self-healed {healed} crashed-publish record(s).")


def _retry_ready(rec: dict) -> bool:
    next_attempt = rec.get("next_attempt_at")
    if not next_attempt:
        return True
    try:
        scheduled = datetime.datetime.fromisoformat(next_attempt)
        if scheduled.tzinfo is None:
            scheduled = scheduled.astimezone()
        return scheduled <= datetime.datetime.now().astimezone()
    except (TypeError, ValueError):
        return True


def _acquire_lock():
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(LOCK), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def process_once(quiet_empty: bool = False) -> int:
    fd = _acquire_lock()
    if fd is None:
        print(f"[{_ts()}] ⚠️ Another publish worker is running; skipping this run")
        return 0
    try:
        # P2-5: surface any records a previous crashed run left mid-publish.
        _check_processing_leftovers()
        records = _load_pending()
        if not records:
            if not quiet_empty:
                print(f"[{_ts()}] Queue empty. Nothing to publish.")
            return 0
        processed = 0
        for rec in records:
            od = rec.get("output_dir", "")
            if od in _done_set():
                _rebuild_pending_excluding(od)
                continue  # safety: never double-publish
            if not _retry_ready(rec):
                continue
            # P2-5: pull out of pending BEFORE publishing so a crash between
            # draft creation and marking-done cannot re-enqueue it (no dup draft).
            _mark_processing(rec)
            try:
                media_id, _ = _publish_one(rec)
                # R2: persist the media_id into the in-progress marker so a crash
                # before the next line (move to done) can be self-healed without
                # creating a duplicate draft.
                _patch_processing_media_id(od, media_id)
                _move(rec, DONE, {"published": False, "media_id": media_id,
                                  "finished_at": _ts()})
                print(f"[{_ts()}] ✅ Draft created: {od} (media_id={media_id[:12]}...)")
                processed += 1
            except Exception as e:
                attempts = int(rec.get("attempts", 0)) + 1
                error = str(e)[:500]
                if attempts < MAX_ATTEMPTS:
                    delay = BASE_RETRY_SECONDS * (2 ** (attempts - 1))
                    next_attempt = datetime.datetime.now().astimezone() + datetime.timedelta(seconds=delay)
                    _move(
                        rec,
                        PENDING,
                        {
                            "attempts": attempts,
                            "last_error": error,
                            "next_attempt_at": next_attempt.isoformat(timespec="seconds"),
                        },
                    )
                    print(
                        f"[{_ts()}] 🔁 Retry {attempts}/{MAX_ATTEMPTS - 1} "
                        f"scheduled in {delay}s: {od}: {error}"
                    )
                else:
                    _move(
                        rec,
                        FAILED,
                        {"attempts": attempts, "error": error, "finished_at": _ts()},
                    )
                    print(f"[{_ts()}] ❌ Failed permanently after {attempts} attempts: {od}: {error}")
            finally:
                _unmark_processing(rec)
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
    parser.add_argument("--quiet-empty", action="store_true", help="Do not log empty queue passes")
    args = parser.parse_args()

    if args.daemon:
        print(f"[{_ts()}] Publish worker daemon started (interval={args.interval}s)")
        while True:
            try:
                process_once(quiet_empty=args.quiet_empty)
            except Exception as e:
                print(f"[{_ts()}] worker error: {e}")
            time.sleep(args.interval)
    else:
        n = process_once(quiet_empty=args.quiet_empty)
        if n or not args.quiet_empty:
            print(f"[{_ts()}] Publish worker done. Processed {n} article(s).")


if __name__ == "__main__":
    main()
