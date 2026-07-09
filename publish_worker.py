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
import re
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
QUEUE_DIR = SCRIPT_DIR / "queue"
PENDING = QUEUE_DIR / "pending.jsonl"
DONE = QUEUE_DIR / "done.jsonl"
FAILED = QUEUE_DIR / "failed.jsonl"
PROCESSING = QUEUE_DIR / "processing.jsonl"  # P2-5: in-progress marker
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
    # P1-1: parse the structured marker wechat_publish.py prints, instead
    # of loosely matching "Draft created" (which also appears in create_draft()
    # with the title, polluting the media_id value).
    media_id = ""
    m = re.search(r"DRAFT_MEDIA_ID=(\S+)", out)
    if m:
        media_id = m.group(1)
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


def _load_processing() -> list:
    if not PROCESSING.exists():
        return []
    recs = []
    for line in PROCESSING.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            recs.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return recs


def _rebuild_pending_excluding(output_dir: str):
    """Rewrite pending.jsonl dropping a single output_dir (atomic-ish).

    P2-5: used to pull a record out of pending *before* publishing it, so a
    crash between draft creation and marking-done cannot re-enqueue the same
    article and create a duplicate WeChat draft.
    """
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    if not PENDING.exists():
        return
    kept = []
    for line in PENDING.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            kept.append(line)
            continue
        if r.get("output_dir") == output_dir:
            continue
        kept.append(line)
    with PENDING.open("w", encoding="utf-8") as f:
        for l in kept:
            f.write(l + "\n")


def _mark_processing(rec: dict):
    """Move a record from pending into the in-progress file."""
    _rebuild_pending_excluding(rec.get("output_dir", ""))
    with PROCESSING.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _unmark_processing(rec: dict):
    """Remove a record from the in-progress file after it is resolved."""
    od = rec.get("output_dir", "")
    recs = [r for r in _load_processing() if r.get("output_dir") != od]
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    with PROCESSING.open("w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _patch_processing_media_id(output_dir: str, media_id: str):
    """Rewrite the in-progress record for output_dir to carry its media_id.

    R2: lets a crash between draft creation and _move() to done be self-healed
    (reuse the media_id) instead of leaving a stuck/duplicate entry.
    """
    recs = _load_processing()
    changed = False
    for r in recs:
        if r.get("output_dir") == output_dir:
            r["media_id"] = media_id
            changed = True
    if changed:
        QUEUE_DIR.mkdir(parents=True, exist_ok=True)
        with PROCESSING.open("w", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")


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
        PROCESSING.write_text("", encoding="utf-8")
        print(f"[{_ts()}] 🩹 Self-healed {healed} crashed-publish record(s).")


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
        # P2-5: surface any records a previous crashed run left mid-publish.
        _check_processing_leftovers()
        records = _load_pending()
        if not records:
            print(f"[{_ts()}] Queue empty. Nothing to publish.")
            return 0
        processed = 0
        for rec in records:
            od = rec.get("output_dir", "")
            if od in _done_set():
                continue  # safety: never double-publish
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
                _move(rec, FAILED, {"error": str(e)[:500], "finished_at": _ts()})
                print(f"[{_ts()}] ❌ Failed: {od}: {e}")
            finally:
                _unmark_processing(rec)
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
