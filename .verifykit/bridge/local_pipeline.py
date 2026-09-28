#!/usr/bin/env python3
"""Local Python side of the VerifyKit bridge for the WeChat article pipeline.

VerifyKit's runtime loader is ESM-only (Node), so ``pipeline.mjs`` shells out to
this script. It invokes the real, deterministic Python business functions
(article pool, candidate selection, queue enqueue) against an isolated temporary
workspace.

Guarantees:

- No network calls. The LLM-backed stages (tagging, scoring, writing) are not
  invoked here; they are covered by the M5 live checker and by offline unit
  tests. Local execution is never reported as third-party evidence.
- No writes outside ``--workspace``. The queue module is redirected to the temp
  workspace and verified before any enqueue.

Each invocation performs exactly one step and prints one JSON object on the last
line of stdout.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BRIDGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BRIDGE_DIR.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

LONG_BODY = " ".join(
    [
        (
            "enterprise workflow redesign manager incentives trust governance "
            "adoption resistance permissions measured cost savings"
        )
    ]
    * 40
)

FIXTURE_MATERIALS = [
    {
        "url": "https://bridge.local/ai-landing-stalls",
        "canonical_url": "https://bridge.local/ai-landing-stalls",
        "title": "Why enterprise AI landing stalls",
        "summary": "workflow redesign and manager incentives",
        "content_text": LONG_BODY,
        "source_type": "rss",
        "source_name": "VerifyKit Bridge Fixture",
        "editorial_fit_score": 0.9,
        "quality": {"content_chars": len(LONG_BODY)},
        "execution_detail_score": 0.8,
        "novelty_score": 0.7,
        "dedup": {"cluster_id": "bridge-c1"},
    },
    {
        "url": "https://bridge.local/ai-roi-measurement",
        "canonical_url": "https://bridge.local/ai-roi-measurement",
        "title": "Measuring AI ROI before scaling",
        "summary": "measured automation rate and cost savings after go live",
        "content_text": LONG_BODY,
        "source_type": "rss",
        "source_name": "VerifyKit Bridge Fixture",
        "editorial_fit_score": 0.88,
        "quality": {"content_chars": len(LONG_BODY)},
        "execution_detail_score": 0.9,
        "novelty_score": 0.6,
        "dedup": {"cluster_id": "bridge-c2"},
    },
]


def _state_path(workspace: Path) -> Path:
    return workspace / "bridge_state.json"


def _write_state(workspace: Path, payload: dict) -> None:
    _state_path(workspace).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def _read_state(workspace: Path) -> dict:
    path = _state_path(workspace)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def step_pool(workspace: Path, date_str: str) -> dict:
    from content_platform.datasets.article_pool import build_article_pool

    pool = build_article_pool(FIXTURE_MATERIALS, topic_memory={"recent_outputs": []})
    candidates = pool["candidates"]
    _write_state(workspace, {"candidates": candidates})
    return {
        "step": "material_pool",
        "candidate_count": len(candidates),
        "candidates": [
            {"index": i + 1, "title": c.get("title", ""), "cluster": c.get("dedup", {}).get("cluster_id", "")}
            for i, c in enumerate(candidates)
        ],
    }


def step_select(workspace: Path, date_str: str) -> dict:
    from content_platform.business.daily_article import topic_planner

    # Point the planner at the isolated workspace so it reads an empty recent
    # ledger and never calls the semantic LLM check.
    topic_planner.WORKSPACE_DIR = workspace

    candidates = _read_state(workspace).get("candidates", [])
    by_url = {c.get("canonical_url") or c.get("url"): i + 1 for i, c in enumerate(candidates)}
    selected = topic_planner.pick_top_n_clusters(candidates, n=2)
    ranked = [by_url.get(s.get("canonical_url") or s.get("url")) for s in selected]
    ranked = [index for index in ranked if index]
    _write_state(workspace, {**_read_state(workspace), "selected": selected, "ranked": ranked})
    return {"step": "candidate_selection", "selected_count": len(selected), "ranked": ranked}


def step_enqueue(workspace: Path, date_str: str) -> dict:
    import publish_queue

    queue_dir = workspace / "queue"
    publish_queue.QUEUE_DIR = queue_dir
    publish_queue.PENDING = queue_dir / "pending.jsonl"
    publish_queue.PROCESSING = queue_dir / "processing.jsonl"
    publish_queue.DONE = queue_dir / "done.jsonl"
    publish_queue.FAILED = queue_dir / "failed.jsonl"
    publish_queue.QUEUE_LOCK = queue_dir / ".queue.lock"

    resolved_pending = Path(publish_queue.PENDING).resolve()
    if workspace.resolve() not in resolved_pending.parents:
        raise SystemExit(
            f"refusing to enqueue outside the bridge workspace: {resolved_pending}"
        )

    selected = _read_state(workspace).get("selected", [])
    enqueued = 0
    duplicates = 0
    for i, material in enumerate(selected):
        output_dir = workspace / "output" / f"article-{i + 1}"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "article.md").write_text(
            f"# Bridge article {i + 1}\n\nlocal verification fixture\n", encoding="utf-8"
        )
        if publish_queue.enqueue_for_publish(str(output_dir), digest=""):
            enqueued += 1
        else:
            duplicates += 1
    # Second enqueue of the same article must be a no-op (idempotency evidence).
    repeat_duplicate = None
    if selected:
        first_dir = workspace / "output" / "article-1"
        repeat_duplicate = publish_queue.enqueue_for_publish(str(first_dir), digest="")
    return {
        "step": "queue_enqueue",
        "enqueued": enqueued,
        "duplicates": duplicates,
        "idempotent_repeat": repeat_duplicate is False,
    }


STEPS = {
    "pool": step_pool,
    "select": step_select,
    "enqueue": step_enqueue,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="VerifyKit local pipeline bridge step")
    parser.add_argument("--step", required=True, choices=sorted(STEPS))
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--date", default="2026-09-28")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    result = STEPS[args.step](workspace, args.date)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
