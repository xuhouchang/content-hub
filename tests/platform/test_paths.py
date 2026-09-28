from pathlib import Path

from content_platform.paths import PlatformPaths


def test_platform_paths_are_rooted_under_workspace(tmp_path: Path):
    paths = PlatformPaths.from_workspace(tmp_path)

    assert paths.platform_dir == tmp_path / "platform"
    assert (
        paths.ingest_raw_dir("2026-06-03")
        == tmp_path / "platform" / "ingest" / "raw" / "2026-06-03"
    )


def test_platform_cli_exposes_expected_jobs():
    from content_platform.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["run", "collect-daily", "--date", "2026-06-03"])

    assert args.command == "run"
    assert args.job_name == "collect-daily"
    assert args.date == "2026-06-03"


def test_platform_cli_main_dispatches_collect_job(tmp_path: Path, monkeypatch):
    import content_platform.runtime as runtime_module
    from platform_cli import main

    # Offline: no live source collectors during the CLI dispatch test.
    for loader in (
        "load_rss_materials",
        "load_reddit_materials",
        "load_blog_materials",
        "load_consulting_materials",
        "load_case_materials",
    ):
        monkeypatch.setattr(runtime_module, loader, lambda date_str: [])

    exit_code = main(
        [
            "run",
            "collect-daily",
            "--date",
            "2026-06-03",
            "--workspace-dir",
            str(tmp_path),
        ]
    )

    assert exit_code == 0
    assert (
        tmp_path / "platform" / "jobs" / "2026-06-03" / "collect-daily" / "job.json"
    ).exists()


def test_platform_cli_main_dispatches_article_job(tmp_path: Path):
    from platform_cli import main
    materials_file = tmp_path / "materials.json"
    materials_file.write_text("[]", encoding="utf-8")

    exit_code = main(
        [
            "run",
            "article-daily",
            "--date",
            "2026-06-03",
            "--workspace-dir",
            str(tmp_path),
            "--materials-file",
            str(materials_file),
        ]
    )

    assert exit_code == 1
    assert (
        tmp_path / "platform" / "jobs" / "2026-06-03" / "article-daily" / "job.json"
    ).exists()
