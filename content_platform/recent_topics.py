"""Typed recent-topic history with a shared cross-format URL dedup view."""

from __future__ import annotations

import datetime
import fcntl
import json
from pathlib import Path


LEGACY_FILE_NAME = "_recent_topics.json"
LEDGER_FILES = {
    "article": "_article_topics.json",
    "case": "_case_topics.json",
}


def _read_list(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return payload if isinstance(payload, list) else []


def _write_list(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(entries[-100:], ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _entry_date(entry: dict) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(str(entry.get("date", "")))
    except ValueError:
        return None


def append_topic(workspace_dir: Path, kind: str, entry: dict) -> None:
    if kind not in LEDGER_FILES:
        raise ValueError(f"Unsupported topic kind: {kind}")
    path = Path(workspace_dir) / "wechat-articles" / LEDGER_FILES[kind]
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            entries = _read_list(path)
            record = dict(entry)
            record["kind"] = kind
            entries.append(record)
            _write_list(path, entries)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def recent_topics(
    workspace_dir: Path,
    kind: str,
    days: int = 7,
    include_legacy_articles: bool = True,
) -> list[dict]:
    """Return a typed topic window; legacy mixed records are article-only when identifiable."""
    if kind not in LEDGER_FILES:
        raise ValueError(f"Unsupported topic kind: {kind}")
    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=days)
    root = Path(workspace_dir) / "wechat-articles"
    entries = [
        entry
        for entry in _read_list(root / LEDGER_FILES[kind])
        if (_entry_date(entry) or datetime.date.min) >= cutoff
    ]

    # The old combined ledger has no type field. Article records normally carry
    # cluster IDs; legacy case records intentionally carried none. Keep only
    # identifiable legacy article topics during the transition.
    if kind == "article" and include_legacy_articles:
        for entry in _read_list(root / LEGACY_FILE_NAME):
            if entry.get("kind") not in (None, "article"):
                continue
            if not entry.get("cluster_ids"):
                continue
            if (_entry_date(entry) or datetime.date.min) >= cutoff:
                entries.append(entry)
    return entries


def recent_source_urls(
    workspace_dir: Path,
    days: int | None = None,
    include_legacy: bool = True,
) -> set[str]:
    """Return source URLs across both formats for cross-format duplicate prevention."""
    root = Path(workspace_dir) / "wechat-articles"
    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=days) if days is not None else None
    paths = [root / filename for filename in LEDGER_FILES.values()]
    if include_legacy:
        paths.append(root / LEGACY_FILE_NAME)
    urls: set[str] = set()
    for path in paths:
        for entry in _read_list(path):
            used_on = _entry_date(entry)
            if cutoff is not None and (used_on is None or used_on < cutoff):
                continue
            for url in entry.get("source_urls", []):
                if url:
                    urls.add(str(url).rstrip("/").lower())
    return urls


def recent_cluster_ids(workspace_dir: Path, kind: str, days: int = 7) -> set[str]:
    return {
        cluster_id
        for entry in recent_topics(workspace_dir, kind, days=days)
        for cluster_id in entry.get("cluster_ids", [])
        if cluster_id
    }
