#!/usr/bin/env python3
"""M5 live acceptance adapter — a single, opt-in real-dependency probe.

This module is invoked only by ``.verifykit/bridge/pipeline.mjs`` and only when
the caller sets ``M5_LIVE_APPROVED=1``. It is default OFF: importing it, running
``verifykit check``, the offline pytest suite, and a gate-less ``verifykit run``
never open a network connection.

Two independent, at-most-once probes are available:

* ``deepseek`` — one POST to the configured DeepSeek chat-completions endpoint
  with ``max_tokens=128``, a 30s urlopen timeout and a 64 KiB response cap. No
  retries. Only provider-issued metadata is returned: never the prompt, the full
  reply, the credential or a raw error body. The same redacted metadata is
  durably written to ``.verifykit/data/m5/<run-id>/deepseek/result.json`` (an
  ignored path) on both the success and failure paths.
* ``wechat`` — one WeChat draft creation through the real ``publish_worker``
  state machine, pointed at an isolated queue under
  ``.verifykit/data/m5/<run-id>/``. The real ``queue/pending.jsonl`` is never
  touched. A permanent cover media id is reused; image upload, dynamic cover
  generation and auto group-send are skipped.

Which probes run is controlled by two gates: ``M5_LIVE_APPROVED=1`` (default
OFF) and ``M5_LIVE_STEPS`` (a comma list, default ``"deepseek,wechat"``). A live
step runs only when both allow it, so an approved DeepSeek-only run with
``M5_LIVE_STEPS=deepseek`` never performs a WeChat draft call.

At-most-once is enforced with ``O_CREAT|O_EXCL`` attempt markers written before
any outbound request. The script prints one JSON object on the last stdout
line. Classified failures are reported as ``ok: false`` plus an ``error_class``
so the Node bridge can call ``kit.degrade()`` instead of inventing success.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT_DEFAULT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT_DEFAULT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT_DEFAULT))

DEEPSEEK_URL_DEFAULT = "https://api.deepseek.com/chat/completions"
DEEPSEEK_PROMPT = "Reply with exactly M5-OK and nothing else."
DEEPSEEK_MAX_TOKENS = 128
DEEPSEEK_TIMEOUT_SECONDS = 30
DEEPSEEK_MAX_RESPONSE_BYTES = 64 * 1024

# Indirection so tests can inject a fake transport without touching urllib.
urlopen = urllib.request.urlopen

# The durable per-run artifact schema. It deliberately omits ``ok`` (a derived
# convenience flag) and always carries an explicit ``cost`` of ``"unknown"``:
# the probe never estimates cost.
DEEPSEEK_ARTIFACT_FIELDS = (
    "step",
    "attempted",
    "request_model_id",
    "response_model_id",
    "auth_class",
    "http_status",
    "content_non_empty",
    "elapsed_ms",
    "usage",
    "cost",
    "provider_request_id",
    "error_class",
)

DEEPSEEK_RESULT_FIELDS = (
    "step",
    "attempted",
    "ok",
    "request_model_id",
    "response_model_id",
    "http_status",
    "auth_class",
    "content_non_empty",
    "elapsed_ms",
    "usage",
    "provider_request_id",
    "error_class",
)

WECHAT_RESULT_FIELDS = (
    "step",
    "attempted",
    "ok",
    "draft_created",
    "media_id",
    "media_id_matches",
    "published",
    "done_records",
    "isolated_workspace",
    "error_class",
)


def live_enabled(env: dict) -> bool:
    """True only for an explicit ``M5_LIVE_APPROVED=1``."""
    return str(env.get("M5_LIVE_APPROVED", "")) == "1"


DEFAULT_LIVE_STEPS = ("deepseek", "wechat")


def live_steps(env: dict) -> tuple:
    """The selected live probes from ``M5_LIVE_STEPS`` (comma list).

    Unset or blank falls back to ``("deepseek", "wechat")`` so existing runs are
    unchanged. Unknown names are ignored rather than treated as an error.
    """
    raw = str(env.get("M5_LIVE_STEPS", "")).strip()
    if not raw:
        return DEFAULT_LIVE_STEPS
    selected = tuple(part.strip() for part in raw.split(",") if part.strip())
    return selected or DEFAULT_LIVE_STEPS


def live_step_enabled(step: str, env: dict) -> bool:
    """A live step runs only when approved AND explicitly selected."""
    return live_enabled(env) and step in live_steps(env)



def _whitelist(result: dict, fields: tuple) -> dict:
    return {name: result.get(name) for name in fields}


def _load_dotenv(repo_root: Path) -> None:
    """Best-effort .env load; never prints or persists any value."""
    env_path = repo_root / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if value and key not in os.environ:
            os.environ[key] = value


def _m5_dir(repo_root: Path, run_id: str) -> Path:
    safe = "".join(ch for ch in str(run_id) if ch.isalnum() or ch in "-_")[:128]
    return Path(repo_root) / ".verifykit" / "data" / "m5" / (safe or "run")


def _claim_attempt(marker: Path) -> bool:
    """Exclusive-create the one-shot marker. False means it already existed."""
    marker.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f"{time.time():.3f}\n")
    return True


def deepseek_request_body(model: str) -> dict:
    """The exact request body the live probe sends (short, hard-capped)."""
    return {
        "model": model,
        "messages": [{"role": "user", "content": DEEPSEEK_PROMPT}],
        "max_tokens": DEEPSEEK_MAX_TOKENS,
        "temperature": 0,
        "stream": False,
    }


def _classify_transport_error(error: Exception) -> tuple[str, int | None]:
    if isinstance(error, urllib.error.HTTPError):
        if error.code in (401, 403):
            return "auth_rejected", error.code
        return "http_error", error.code
    if isinstance(error, (socket.timeout, TimeoutError)):
        return "timeout", None
    if isinstance(error, urllib.error.URLError) and isinstance(error.reason, (socket.timeout, TimeoutError)):
        return "timeout", None
    return "network_error", None


def _usage_metadata(usage: object) -> object:
    if not isinstance(usage, dict):
        return "unknown"
    values = {
        key: usage[key]
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        if isinstance(usage.get(key), int)
    }
    return values or "unknown"


def _deepseek_url(env: dict) -> str:
    base = str(env.get("DEEPSEEK_BASE", "")).strip().rstrip("/")
    return f"{base}/chat/completions" if base else DEEPSEEK_URL_DEFAULT


def _write_deepseek_artifact(base: Path, result: dict) -> Path:
    """Durably persist the redacted per-run DeepSeek result on an ignored path.

    Only whitelisted, provider-issued metadata is written: never the prompt, the
    full reply, the credential, request headers or a raw error body.
    """
    artifact = {
        "step": "deepseek",
        "attempted": bool(result.get("attempted")),
        "request_model_id": result.get("request_model_id"),
        "response_model_id": result.get("response_model_id"),
        "auth_class": result.get("auth_class"),
        "http_status": result.get("http_status"),
        "content_non_empty": bool(result.get("content_non_empty")),
        "elapsed_ms": result.get("elapsed_ms"),
        "usage": result.get("usage", "unknown"),
        "cost": "unknown",
        "provider_request_id": result.get("provider_request_id"),
        "error_class": result.get("error_class"),
    }
    base.mkdir(parents=True, exist_ok=True)
    path = base / "result.json"
    path.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _probe_deepseek(base: Path, repo_root: Path, env: dict, transport) -> dict:
    """The one-shot probe body; the caller durably records its redacted result."""
    marker = base / "request.attempted"

    result = {
        "step": "deepseek",
        "attempted": False,
        "ok": False,
        "request_model_id": None,
        "response_model_id": None,
        "http_status": None,
        "auth_class": "not_attempted",
        "content_non_empty": False,
        "elapsed_ms": None,
        "usage": "unknown",
        "provider_request_id": None,
        "error_class": None,
    }

    if not _claim_attempt(marker):
        result["error_class"] = "already_attempted"
        return result
    result["attempted"] = True

    _load_dotenv(repo_root)
    api_key = env.get("DEEPSEEK_API_KEY", "") or env.get("LLM_API_KEY", "")
    model = str(env.get("DEEPSEEK_MODEL", "")).strip() or "deepseek-flash"
    result["request_model_id"] = model
    if not api_key:
        result["auth_class"] = "not_configured"
        result["error_class"] = "not_configured"
        return result

    request = urllib.request.Request(
        _deepseek_url(env),
        data=json.dumps(deepseek_request_body(model)).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    do_open = transport or urlopen
    started = time.monotonic()

    try:
        with do_open(request, timeout=DEEPSEEK_TIMEOUT_SECONDS) as response:
            raw = response.read(DEEPSEEK_MAX_RESPONSE_BYTES)
            headers = response.headers
            status = getattr(response, "status", None) or response.getcode()
    except Exception as error:  # noqa: BLE001 - classified and reported, never swallowed
        auth_class, http_status = _classify_transport_error(error)
        result["auth_class"] = auth_class
        result["http_status"] = http_status
        result["error_class"] = auth_class
        result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return result

    result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    result["http_status"] = status
    result["auth_class"] = "ok"

    try:
        payload = json.loads(raw.decode("utf-8"))
    # verifykit-allow: no-unified-fallback-bypass unparseable JSON is a classified provider error (error_class)
    except (json.JSONDecodeError, UnicodeDecodeError):
        result["error_class"] = "unparseable_response"
        return result
    if not isinstance(payload, dict):
        result["error_class"] = "unexpected_response_shape"
        return result

    if isinstance(payload.get("model"), str) and payload["model"]:
        result["response_model_id"] = payload["model"]

    # A provider-issued request id only. The response body ``id`` and the
    # ``x-request-id`` header both qualify; nothing is ever synthesized.
    provider_id = payload.get("id")
    if not (isinstance(provider_id, str) and provider_id):
        header_id = headers.get("x-request-id") if headers else None
        provider_id = header_id if isinstance(header_id, str) and header_id else None
    result["provider_request_id"] = provider_id

    result["usage"] = _usage_metadata(payload.get("usage"))
    choices = payload.get("choices")
    content = ""
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict):
            content = message.get("content") or ""
    result["content_non_empty"] = bool(isinstance(content, str) and content.strip())

    if not result["content_non_empty"]:
        result["error_class"] = "empty_content"
        return result

    result["ok"] = True
    return result


def run_deepseek_step(repo_root: Path, run_id: str, *, transport=None, env=None) -> dict:
    """One capped DeepSeek chat-completion request. At most once per run id."""
    repo_root = Path(repo_root)
    env = os.environ if env is None else env
    base = _m5_dir(repo_root, run_id) / "deepseek"

    result = _probe_deepseek(base, repo_root, env, transport)

    # Write on both success and failure. An ``already_attempted`` re-entry must
    # not clobber the durable artifact written by the first attempt; it only
    # writes if, for some reason, no artifact exists yet.
    artifact_path = base / "result.json"
    if result.get("error_class") != "already_attempted" or not artifact_path.exists():
        _write_deepseek_artifact(base, result)

    return _whitelist(result, DEEPSEEK_RESULT_FIELDS)


def cover_media_id(title: str) -> str:
    """Reuse an existing permanent cover media id from the repository."""
    import wechat_publish

    combined = (title or "").lower()
    cover_type = "report" if any(
        keyword in combined
        for keyword in ("报告", "report", "白皮书", "深度", "解读", "analysis", "index", "趋势")
    ) else "news"
    covers = wechat_publish.PERMANENT_COVERS
    return covers.get(f"{cover_type}-1x1") or covers.get(cover_type) or covers.get("news-1x1", "")


def _build_publisher():
    import wechat_publish

    if not wechat_publish.WECHAT_APP_ID or not wechat_publish.WECHAT_APP_SECRET:
        raise RuntimeError("wechat credentials are not configured")
    return wechat_publish.WeChatPublisher(
        wechat_publish.WECHAT_APP_ID, wechat_publish.WECHAT_APP_SECRET
    )


_QUEUE_GLOBAL_NAMES = ("QUEUE_DIR", "PENDING", "PROCESSING", "DONE", "FAILED", "QUEUE_LOCK")
_WORKER_GLOBAL_NAMES = ("QUEUE_DIR", "PENDING", "PROCESSING", "DONE", "FAILED", "LOCK", "SCRIPT_DIR")


def _snapshot_queue_globals(publish_queue, publish_worker) -> dict:
    return {
        "queue": {name: getattr(publish_queue, name) for name in _QUEUE_GLOBAL_NAMES},
        "worker": {name: getattr(publish_worker, name) for name in _WORKER_GLOBAL_NAMES},
    }


def _redirect_queue_globals(publish_queue, publish_worker, base: Path) -> None:
    queue_dir = base / "queue"
    paths = {
        "QUEUE_DIR": queue_dir,
        "PENDING": queue_dir / "pending.jsonl",
        "PROCESSING": queue_dir / "processing.jsonl",
        "DONE": queue_dir / "done.jsonl",
        "FAILED": queue_dir / "failed.jsonl",
    }
    for name, value in paths.items():
        setattr(publish_queue, name, value)
        setattr(publish_worker, name, value)
    publish_queue.QUEUE_LOCK = queue_dir / ".queue.lock"
    publish_worker.LOCK = queue_dir / ".worker.lock"
    publish_worker.SCRIPT_DIR = base


def _restore_queue_globals(publish_queue, publish_worker, saved: dict) -> None:
    for name, value in saved["queue"].items():
        setattr(publish_queue, name, value)
    for name, value in saved["worker"].items():
        setattr(publish_worker, name, value)


def _safe_error_class(error: Exception) -> str:
    if isinstance(error, (socket.timeout, TimeoutError)):
        return "timeout"
    return "publish_failed"


def run_wechat_step(
    repo_root: Path,
    run_id: str,
    *,
    article_src: Path | None = None,
    publisher_factory=None,
) -> dict:
    """One WeChat draft creation against an isolated queue. At most once."""
    repo_root = Path(repo_root)
    base = _m5_dir(repo_root, run_id) / "wechat"
    marker = base / "draft.attempted"

    result = {
        "step": "wechat",
        "attempted": False,
        "ok": False,
        "draft_created": False,
        "media_id": None,
        "media_id_matches": False,
        "published": None,
        "done_records": 0,
        "isolated_workspace": str(base),
        "error_class": None,
    }

    # Credential presence is checked before the marker: no credential means no
    # draft request was ever made, so a later configured run may still proceed.
    if publisher_factory is None:
        import wechat_publish

        if not wechat_publish.WECHAT_APP_ID or not wechat_publish.WECHAT_APP_SECRET:
            result["error_class"] = "wechat_not_configured"
            return _whitelist(result, WECHAT_RESULT_FIELDS)

    article_source = (
        Path(article_src)
        if article_src
        else repo_root / "docs" / "acceptance" / "m5-test-article.md"
    )
    if not article_source.exists():
        result["error_class"] = "test_article_missing"
        return _whitelist(result, WECHAT_RESULT_FIELDS)

    if not _claim_attempt(marker):
        result["error_class"] = "already_attempted"
        return _whitelist(result, WECHAT_RESULT_FIELDS)
    result["attempted"] = True

    article_dir = base / "article"
    article_dir.mkdir(parents=True, exist_ok=True)
    (article_dir / "article.md").write_text(
        article_source.read_text(encoding="utf-8"), encoding="utf-8"
    )

    factory = publisher_factory or _build_publisher

    import publish_queue
    import publish_worker

    saved = _snapshot_queue_globals(publish_queue, publish_worker)
    original_publish_one = publish_worker._publish_one
    holder = {"media_id": None, "thumb_media_id": None}
    try:
        _redirect_queue_globals(publish_queue, publish_worker, base)
        publish_queue.enqueue_for_publish(str(article_dir), digest="")

        def _draft_only(rec):
            publisher = factory()
            article_path = Path(rec["output_dir"]) / rec.get("article", "article.md")
            parsed = publisher.parse_markdown_article(str(article_path))
            thumb = cover_media_id(parsed.get("title", ""))
            holder["thumb_media_id"] = thumb
            media_id = publisher.create_draft(parsed, thumb)
            if not media_id:
                raise RuntimeError("create_draft returned no media_id")
            holder["media_id"] = media_id
            return media_id, ""

        publish_worker._publish_one = _draft_only
        processed = publish_worker.process_once(quiet_empty=True)

        done = [
            record
            for record in publish_queue.read_records_unlocked(publish_queue.DONE)
            if str(Path(record.get("output_dir", "")).resolve()) == str(article_dir.resolve())
        ]
        result["done_records"] = len(done)
        if done:
            result["published"] = done[0].get("published")
            result["media_id"] = done[0].get("media_id")
            result["media_id_matches"] = bool(holder["media_id"]) and done[0].get(
                "media_id"
            ) == holder["media_id"]
        result["draft_created"] = bool(holder["media_id"])
        result["ok"] = (
            processed == 1
            and len(done) == 1
            and result["done_records"] == 1
            and result["published"] is False
            and result["media_id_matches"] is True
        )
        if not result["ok"]:
            result["error_class"] = "publish_failed"
    except Exception as error:  # noqa: BLE001 - classified, never swallowed
        result["ok"] = False
        result["error_class"] = _safe_error_class(error)
    finally:
        publish_worker._publish_one = original_publish_one
        _restore_queue_globals(publish_queue, publish_worker, saved)

    return _whitelist(result, WECHAT_RESULT_FIELDS)


def run_step(step: str, repo_root: Path, run_id: str, *, env=None, **kwargs) -> dict:
    env = os.environ if env is None else env
    if not live_enabled(env):
        return {"step": step, "skipped": True, "reason": "M5_LIVE_APPROVED!=1"}
    if step not in live_steps(env):
        return {"step": step, "skipped": True, "reason": f"M5_LIVE_STEPS excludes {step}"}
    if step == "deepseek":
        return run_deepseek_step(repo_root, run_id, env=env, **kwargs)
    if step == "wechat":
        return run_wechat_step(repo_root, run_id, **kwargs)
    return {"step": step, "skipped": False, "ok": False, "error_class": "unknown_step"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M5 live acceptance adapter (default OFF)")
    parser.add_argument("--step", required=True, choices=["deepseek", "wechat"])
    parser.add_argument("--repo-root", default=str(REPO_ROOT_DEFAULT))
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)

    try:
        result = run_step(args.step, Path(args.repo_root), args.run_id)
    except Exception as error:  # noqa: BLE001 - surfaced as a classified failure
        result = {
            "step": args.step,
            "skipped": False,
            "ok": False,
            "error_class": "unexpected_error",
            "error_type": type(error).__name__,
        }
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
