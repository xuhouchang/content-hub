from pathlib import Path
import json

import decompose_case_study
import write_article


def test_log_article_topic_can_skip_all_urls_registry(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(write_article, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(write_article, "OUTPUT_BASE", tmp_path / "wechat-articles")
    ledger = tmp_path / "wechat-articles" / "_article_topics.json"

    write_article._log_article_topic(
        title="Workflow redesign in support ops",
        digest="A focused summary",
        output_dir=tmp_path / "wechat-articles" / "2026-06-03-example",
        source_urls=["https://example.com/article"],
        mark_source_urls=False,
    )

    assert ledger.exists()
    assert not (tmp_path / "reports" / "_index" / "all_urls.tsv").exists()


def test_case_main_with_materials_does_not_touch_external_used_registry(tmp_path: Path, monkeypatch):
    materials_file = tmp_path / "case_materials.json"
    materials_file.write_text(
        json.dumps([{"title": "Case", "url": "https://example.com/case", "content": "detail"}]),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        decompose_case_study,
        "EXTERNAL_USED_FILE",
        tmp_path / ".state" / "external_used_urls.txt",
    )
    monkeypatch.setattr(
        decompose_case_study,
        "write_case",
        lambda date_str, materials, dry_run=False: str(tmp_path / "out"),
    )
    monkeypatch.setattr(decompose_case_study, "post_process", lambda output_dir: True)
    monkeypatch.setattr(
        decompose_case_study.sys,
        "argv",
        [
            "decompose_case_study.py",
            "--date",
            "2026-06-03",
            "--materials",
            str(materials_file),
        ],
    )

    exit_code = decompose_case_study.main()

    assert exit_code == 0
    assert not decompose_case_study.EXTERNAL_USED_FILE.exists()
