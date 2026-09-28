#!/usr/bin/env python3
"""M1 offline scenarios: final-draft acceptance and commit order.

Four required scenarios (all offline, no network, no live writes):

1. polish success  -> qualified final draft enters the queue exactly once.
2. polish failure  -> writer failure; no topic/URL record, no queue entry.
3. quality drop    -> final rejection; no topic/URL record, no queue entry.
4. queue failure   -> commit recorded incomplete, never reported successful.

Plus the semantic-review opt-in and daily review budget.
"""

import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest

import publish_queue
import write_article as wa


GOOD_FINAL = """# 组织跟不上才是AI落地的真正瓶颈

摘要: 真正卡住AI落地的不是技术。

过去一年，73%的企业在部署AI后仍面临决策瓶颈。数据分散导致判断滞后，进而让试点项目无法复制。因为决策链没有重构，所以即使工具再先进，一线也用不起来。这因此形成了"有工具没结果"的死循环。

不过，这套判断并不适用于单独开发者——没有组织层变量时，个人工具的ROI依然直接。研究来源：https://example.com/a；另一篇：https://example.com/b。

另一个代价是：重构决策链需要跨越部门利益，这往往是最大的隐性成本。
"""

SOURCE_MAP = [{"title": "src", "source_urls": ["https://example.com/a", "https://example.com/b"]}]


def _output_dir(tmp_path: Path, name: str = "2026-09-28-bottleneck") -> Path:
    output_dir = tmp_path / "wechat-articles" / name
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "article.md").write_text(GOOD_FINAL, encoding="utf-8")
    return output_dir


def _records(path: Path) -> list[dict]:
    return publish_queue.read_records_unlocked(path)


def test_polish_success_enters_queue_once(tmp_path: Path):
    output_dir = _output_dir(tmp_path)

    quality = wa.evaluate_final_draft(
        GOOD_FINAL, title="组织跟不上才是AI落地的真正瓶颈",
        source_map=SOURCE_MAP, date_str="2026-09-28",
    )
    assert quality["passed"] is True, quality
    assert quality["traceability"]["ok"] is True

    first = wa.commit_final_article(
        output_dir=output_dir, date_str="2026-09-28",
        title="组织跟不上才是AI落地的真正瓶颈", digest="d",
        article_text=GOOD_FINAL, source_map=SOURCE_MAP,
        source_urls=["https://example.com/a", "https://example.com/b"],
        cluster_ids=["c1"], enqueue=True,
    )
    assert first["committed"] is True, first
    article_id = wa.derive_article_id("2026-09-28", "https://example.com/a")
    assert first["article_id"] == article_id

    pending = _records(publish_queue.PENDING)
    assert len(pending) == 1
    assert pending[0]["article_id"] == article_id

    # Retry the same commit: still exactly one queue entry (idempotent by ID).
    second = wa.commit_final_article(
        output_dir=output_dir, date_str="2026-09-28",
        title="组织跟不上才是AI落地的真正瓶颈", digest="d",
        article_text=GOOD_FINAL, source_map=SOURCE_MAP,
        source_urls=["https://example.com/a", "https://example.com/b"],
        cluster_ids=["c1"], enqueue=True,
    )
    assert second["committed"] is True
    assert len(_records(publish_queue.PENDING)) == 1

    ledger = json.loads((wa.OUTPUT_BASE / "_article_topics.json").read_text(encoding="utf-8"))
    assert len(ledger) == 1
    assert ledger[0]["article_id"] == article_id

    # meta.json carries the stable ID and commit state.
    meta = json.loads((output_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["article_id"] == article_id
    assert meta["commit"]["committed"] is True


def test_polish_failure_retains_draft_and_blocks_commit(tmp_path: Path, monkeypatch):
    output_dir = _output_dir(tmp_path)
    article_path = output_dir / "article.md"
    failing = tmp_path / "failing_polish.py"
    failing.write_text("import sys\nsys.exit(7)\n", encoding="utf-8")
    monkeypatch.setattr(wa, "POLISH_SCRIPT", failing)

    error = wa._run_polish_stage(article_path, output_dir)

    assert error != ""
    assert article_path.exists()  # draft retained
    assert (output_dir / "polish_error.txt").exists()
    meta = json.loads((output_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "polish_failed"
    # No commit happened.
    assert _records(publish_queue.PENDING) == []
    assert not (wa.OUTPUT_BASE / "_article_topics.json").exists()


def test_polish_timeout_is_writer_failure(tmp_path: Path, monkeypatch):
    output_dir = _output_dir(tmp_path)
    article_path = output_dir / "article.md"
    monkeypatch.setattr(wa, "POLISH_SCRIPT", tmp_path / "missing.py")
    (tmp_path / "missing.py").write_text("", encoding="utf-8")

    def _timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="polish", timeout=180)

    monkeypatch.setattr(wa.subprocess, "run", _timeout)
    error = wa._run_polish_stage(article_path, output_dir)
    assert "timed out" in error


def test_polish_missing_final_file_is_writer_failure(tmp_path: Path, monkeypatch):
    output_dir = _output_dir(tmp_path)
    article_path = output_dir / "article.md"
    script = tmp_path / "delete_polish.py"
    script.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    monkeypatch.setattr(wa, "POLISH_SCRIPT", script)

    def _run_and_delete(*args, **kwargs):
        article_path.unlink()
        return subprocess.CompletedProcess(args[0], 0, stdout="ok", stderr="")

    monkeypatch.setattr(wa.subprocess, "run", _run_and_delete)
    error = wa._run_polish_stage(article_path, output_dir)
    assert "missing" in error


def test_quality_drop_after_polish_blocks_commit(tmp_path: Path):
    degraded = "# 素材报告解读\n\n原文摘录: 报告说 A。\n\n原文摘录: 报告再说 A。\n\n原文摘录: 报告还是 A。\n"
    quality = wa.evaluate_final_draft(
        degraded, title="素材报告解读", source_map=SOURCE_MAP, date_str="2026-09-28"
    )
    assert quality["passed"] is False
    # The caller only commits when passed; verify no records were written.
    assert _records(publish_queue.PENDING) == []
    assert not (wa.OUTPUT_BASE / "_article_topics.json").exists()


def test_missing_in_body_source_blocks_acceptance(tmp_path: Path):
    # Rules pass, but the final body has no locatable source URL/marker.
    no_source = GOOD_FINAL.replace("研究来源：https://example.com/a；另一篇：https://example.com/b。", "")
    quality = wa.evaluate_final_draft(
        no_source, title="组织跟不上才是AI落地的真正瓶颈",
        source_map=SOURCE_MAP, date_str="2026-09-28",
    )
    assert quality["traceability"]["ok"] is False
    assert quality["passed"] is False
    assert any("可定位" in reason for reason in quality["reasons"])


def test_queue_write_failure_is_not_reported_successful(tmp_path: Path, monkeypatch):
    output_dir = _output_dir(tmp_path)

    def _boom(*args, **kwargs):
        raise OSError("queue disk full")

    monkeypatch.setattr(publish_queue, "enqueue_for_publish", _boom)

    result = wa.commit_final_article(
        output_dir=output_dir, date_str="2026-09-28", title="t", digest="",
        article_text=GOOD_FINAL, source_map=SOURCE_MAP,
        source_urls=["https://example.com/a"], cluster_ids=[], enqueue=True,
    )

    assert result["committed"] is False
    assert result["steps"]["queue"]["status"] == "failed"
    assert result["errors"]
    meta = json.loads((output_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["commit"]["committed"] is False


def test_semantic_review_enabled_but_unavailable_rejects(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("QUALITY_LLM_JUDGE", "1")
    monkeypatch.setattr(
        "lib.quality_judge.evaluate_quality",
        lambda *a, **k: {
            "passed": False, "status": "unavailable", "score": 0, "checks": {},
            "reasons": ["无凭证"], "warnings": [], "llm": None, "rules": {},
        },
    )
    quality = wa.evaluate_final_draft(
        GOOD_FINAL, title="组织跟不上才是AI落地的真正瓶颈",
        source_map=SOURCE_MAP, date_str="2026-09-28", workspace_dir=tmp_path,
    )
    assert quality["passed"] is False
    assert quality["status"] == "unavailable"


def test_semantic_review_daily_budget(tmp_path: Path):
    assert wa.consume_quality_review_budget(tmp_path, "2026-09-28") is True
    assert wa.consume_quality_review_budget(tmp_path, "2026-09-28") is True
    assert wa.consume_quality_review_budget(tmp_path, "2026-09-28") is False


def test_article_id_is_stable_and_normalized(tmp_path: Path):
    a = wa.derive_article_id("2026-09-28", "https://Example.com/Path/")
    b = wa.derive_article_id("2026-09-28", "https://example.com/path")
    assert a == b
    assert wa.derive_article_id("2026-09-29", "https://example.com/path") != a
