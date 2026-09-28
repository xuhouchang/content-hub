"""Tests for the 2026-08-24 fix: override materials must not be re-emptied by
the writer's legacy ``all_urls.tsv`` 'used' filter.

Requirement:
- Cross-day dedup (default path) must still drop materials marked 'used'.
- Same-day selector-provided override materials (skip_used_filter=True) must NOT
  be dropped by the legacy used-status filter.

Both paths must still apply the past-3-day recent-URL filter (short-window
cross-day dedup), so we mock the recent-topics file to be empty of the sample URL.
"""

from pathlib import Path
import json

from lib import materials as M


def _write_all_urls(tmp_path: Path, used_url: str | None = None):
    """Write an all_urls.tsv with an optional 'used' line and a 'collected' line."""
    tsv = tmp_path / "reports" / "_index" / "all_urls.tsv"
    tsv.parent.mkdir(parents=True, exist_ok=True)
    lines = ["https://example.com/fresh\tcollected\t2026-08-24"]
    if used_url:
        lines.append(f"{used_url}\tused\t2026-08-23")
    tsv.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tsv


def _write_empty_recent_topics(tmp_path: Path):
    """An empty recent-topics ledger so the past-3-day URL filter has no hits."""
    f = tmp_path / "wechat-articles" / "_recent_topics.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps([], encoding="utf-8") if False else "[]", encoding="utf-8")
    return f


def _setup(monkeypatch, tmp_path: Path, used_url: str | None):
    _write_empty_recent_topics(tmp_path)
    tsv = _write_all_urls(tmp_path, used_url)
    monkeypatch.setattr(M, "ALL_URLS_FILE", tsv)
    monkeypatch.setattr(M, "WORKSPACE_DIR", tmp_path)


def test_default_path_filters_used_materials(monkeypatch, tmp_path):
    """Cross-day dedup preserved: default path drops materials marked 'used'."""
    used_url = "https://example.com/used-last-week"
    _setup(monkeypatch, tmp_path, used_url=used_url)
    materials = [
        {"url": used_url, "content": "x" * 600},
        {"url": "https://example.com/fresh", "content": "y" * 600},
    ]
    sampled = M.sample_materials(materials, max_count=5)
    urls = [m["url"] for m in sampled]
    assert used_url not in urls, "default path must drop used materials (cross-day dedup)"
    assert "https://example.com/fresh" in urls


def test_override_path_keeps_used_materials(monkeypatch, tmp_path):
    """Same-day override: materials marked 'used' in the legacy registry survive."""
    used_url = "https://example.com/used-last-week"
    _setup(monkeypatch, tmp_path, used_url=used_url)
    materials = [
        {"url": used_url, "content": "x" * 600},
        {"url": "https://example.com/fresh", "content": "y" * 600},
    ]
    sampled = M.sample_materials(materials, max_count=5, skip_used_filter=True)
    urls = [m["url"] for m in sampled]
    assert used_url in urls, "override path must keep selector-chosen materials"


def test_override_still_applies_recent_url_dedup(monkeypatch, tmp_path):
    """Override path still drops URLs used in the past 3 days (short-window dedup)."""
    _setup(monkeypatch, tmp_path, used_url=None)
    recent = tmp_path / "wechat-articles" / "_recent_topics.json"
    recent.write_text(json.dumps([
        {"date": "2026-08-23", "source_urls": ["https://example.com/yesterday"]}
    ]), encoding="utf-8")
    materials = [
        {"url": "https://example.com/yesterday", "content": "x" * 600},
        {"url": "https://example.com/fresh", "content": "y" * 600},
    ]
    sampled = M.sample_materials(materials, max_count=5, skip_used_filter=True)
    urls = [m["url"] for m in sampled]
    assert "https://example.com/yesterday" not in urls
    assert "https://example.com/fresh" in urls
