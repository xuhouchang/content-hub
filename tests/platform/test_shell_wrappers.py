from pathlib import Path


def test_run_all_uses_platform_cli():
    content = Path("run_all.sh").read_text(encoding="utf-8")

    assert "platform_cli.py" in content
    assert "collect-daily" in content
    assert ".venv/bin/python" in content
    assert "poll_and_save.py" not in content


def test_run_daily_article_uses_platform_cli():
    content = Path("run_daily_article.sh").read_text(encoding="utf-8")

    assert "platform_cli.py" in content
    assert "article-daily" in content
    assert ".venv/bin/python" in content
    assert "all_urls.tsv" not in content


def test_run_daily_case_uses_platform_cli():
    content = Path("run_daily_case.sh").read_text(encoding="utf-8")

    assert "platform_cli.py" in content
    assert "case-daily" in content
    assert ".venv/bin/python" in content
    assert "all_urls.tsv" not in content
