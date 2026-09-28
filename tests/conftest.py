"""Global pytest isolation.

The repository keeps live runtime state in repo-root paths (queue/,
wechat-articles/, platform/state/). Several modules resolve those paths from
module-level constants at import time, so a test that exercises the real
pipeline would otherwise mutate the live workspace.

This autouse fixture redirects every one of those module-level paths into a
per-test ``tmp_path`` sandbox. It only patches Python names; no production code
contains a test hook. Tests that set their own paths (e.g. via monkeypatch or
direct attribute assignment) still win, because the fixture runs first.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

WECHAT_PUBLISH_SCRIPT = REPO_ROOT / "wechat_publish.py"


@pytest.fixture(autouse=True)
def isolate_live_workspace(tmp_path, monkeypatch):
    import content_platform.curate.scoring as scoring
    import content_platform.runtime as runtime
    import lib.materials as materials
    import publish_queue
    import publish_worker
    import write_article

    sandbox = tmp_path / "sandbox"
    (sandbox / "wechat-articles").mkdir(parents=True, exist_ok=True)

    # Material score / tag caches (repo-root fixed paths).
    monkeypatch.setattr(
        scoring, "SCORE_CACHE_FILE", sandbox / "wechat-articles" / "_material_scores.json"
    )
    monkeypatch.setattr(
        runtime, "_TAG_FILE", sandbox / "wechat-articles" / "_material_tags.json"
    )

    # lib.materials resolves its workspace-relative ledgers from WORKSPACE_DIR.
    monkeypatch.setattr(materials, "WORKSPACE_DIR", sandbox)

    # Writer output / report registry.
    monkeypatch.setattr(write_article, "OUTPUT_BASE", sandbox / "wechat-articles")
    monkeypatch.setattr(write_article, "REPORTS_DIR", sandbox / "reports")

    # Publish queue (shared by writer, worker, case pipeline).
    queue_dir = sandbox / "queue"
    queue_paths = {
        "QUEUE_DIR": queue_dir,
        "PENDING": queue_dir / "pending.jsonl",
        "PROCESSING": queue_dir / "processing.jsonl",
        "DONE": queue_dir / "done.jsonl",
        "FAILED": queue_dir / "failed.jsonl",
    }
    for name, value in queue_paths.items():
        monkeypatch.setattr(publish_queue, name, value)
        monkeypatch.setattr(publish_worker, name, value)
    monkeypatch.setattr(publish_queue, "QUEUE_LOCK", queue_dir / ".queue.lock")
    monkeypatch.setattr(publish_worker, "LOCK", queue_dir / ".worker.lock")

    # publish_worker records alert health under SCRIPT_DIR; keep the script path
    # but send local alert state into the sandbox.
    monkeypatch.setattr(publish_worker, "SCRIPT_DIR", sandbox)
    monkeypatch.setattr(publish_worker, "WECHAT_PUBLISH", WECHAT_PUBLISH_SCRIPT)

    # Keep the orchestration tests offline: the real runtime calls the DeepSeek
    # API for material tagging and batch scoring. Tests exercise orchestration,
    # not the LLM, so replace those two external calls with deterministic local
    # stubs. Individual tests may override these again.
    def _offline_tag_material(url, title, content_text):
        return None

    def _offline_score_materials_batch(materials):
        scored = []
        for material in materials:
            material = dict(material)
            try:
                score = float(material.get("editorial_fit_score") or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            if score <= 0.0:
                score = 0.9
            material["editorial_fit_score"] = score
            material.setdefault("score_confidence", "offline")
            scored.append(material)
        return scored

    monkeypatch.setattr(runtime, "_llm_tag_material", _offline_tag_material)
    monkeypatch.setattr(runtime, "score_materials_batch", _offline_score_materials_batch)

    yield
