"""M5 offline boundary tests for the live acceptance adapter.

These tests prove the adapter's offline guarantees without any network access:

* the live gate is default-OFF;
* the DeepSeek request is hard-capped and at-most-once;
* a provider-issued request id is recorded only when actually returned;
* no credential, prompt, raw content or raw error body leaks into the result;
* the WeChat probe uses an isolated queue, a permanent cover and no image
  upload, and never touches the real queue.

The real live probes run only in phase 2, after explicit human approval, with
``M5_LIVE_APPROVED=1``.
"""

import io
import json
import os
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BRIDGE_DIR = REPO_ROOT / ".verifykit" / "bridge"
if str(BRIDGE_DIR) not in sys.path:
    sys.path.insert(0, str(BRIDGE_DIR))

import m5_acceptance as m5

import publish_queue
import wechat_publish

ARTICLE_TITLE = "[链路自测] 公众号草稿创建验证"


class _FakeResponse:
    def __init__(self, body: bytes, headers=None, status=200):
        self._body = body
        self.headers = headers or {}
        self.status = status
        self.read_limit = None

    def read(self, limit=-1):
        self.read_limit = limit
        return self._body if limit is None or limit < 0 else self._body[:limit]

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _transport(body: bytes, headers=None, status=200):
    calls = []

    def _open(request, timeout=None):
        calls.append({"request": request, "timeout": timeout})
        return _FakeResponse(body, headers=headers, status=status)

    _open.calls = calls
    return _open


def _deepseek_body(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _ok_payload(**extra):
    payload = {
        "id": "chatcmpl-provider-1",
        "model": "deepseek-chat",
        "choices": [{"message": {"content": "M5-OK"}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13},
    }
    payload.update(extra)
    return payload


class _FakePublisher:
    def __init__(self, media_id="MEDIA-123", fail=False):
        self.media_id = media_id
        self.fail = fail
        self.calls = {"parse": 0, "create": 0}
        self.thumb = None

    def parse_markdown_article(self, article_path, digest=""):
        self.calls["parse"] += 1
        return {"title": ARTICLE_TITLE, "content": f"<p>{ARTICLE_TITLE}</p>"}

    def create_draft(self, article_data, thumb_media_id=""):
        self.calls["create"] += 1
        self.thumb = thumb_media_id
        if self.fail:
            raise RuntimeError("draft endpoint rejected the request")
        return self.media_id

    def upload_image(self, *_args, **_kwargs):
        raise AssertionError("M5 probe must not upload any image")


@pytest.fixture
def deepseek_key(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-real")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
    monkeypatch.delenv("DEEPSEEK_BASE", raising=False)


def _article(tmp_path: Path) -> Path:
    src = tmp_path / "article-src" / "m5-test-article.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(f"# {ARTICLE_TITLE}\n\n链路自测正文。\n", encoding="utf-8")
    return src


# ── live gate ─────────────────────────────────────────────────────────────


def test_live_gate_is_default_off():
    assert m5.live_enabled({}) is False
    assert m5.live_enabled({"M5_LIVE_APPROVED": "0"}) is False
    assert m5.live_enabled({"M5_LIVE_APPROVED": "true"}) is False
    assert m5.live_enabled({"M5_LIVE_APPROVED": "1"}) is True


def test_run_step_skips_without_gate(tmp_path, monkeypatch):
    def _explode(*_args, **_kwargs):
        raise AssertionError("offline run must not open a network connection")

    monkeypatch.setattr(m5, "urlopen", _explode)
    result = m5.run_step("deepseek", tmp_path, "run-gate", env={})
    assert result == {"step": "deepseek", "skipped": True, "reason": "M5_LIVE_APPROVED!=1"}
    result = m5.run_step("wechat", tmp_path, "run-gate", env={"M5_LIVE_APPROVED": "0"})
    assert result["skipped"] is True


def test_live_steps_defaults_and_override():
    assert m5.live_steps({}) == ("deepseek", "wechat")
    assert m5.live_steps({"M5_LIVE_STEPS": ""}) == ("deepseek", "wechat")
    assert m5.live_steps({"M5_LIVE_STEPS": "wechat"}) == ("wechat",)
    assert m5.live_steps({"M5_LIVE_STEPS": " deepseek , wechat "}) == ("deepseek", "wechat")
    # An all-blank list falls back to the default rather than disabling both.
    assert m5.live_steps({"M5_LIVE_STEPS": " , "}) == ("deepseek", "wechat")


def test_run_step_respects_live_steps_subset(tmp_path, monkeypatch):
    """An approved run selects probes by name; an unselected probe never runs."""

    def _explode(*_args, **_kwargs):
        raise AssertionError("an unselected live step must not run")

    monkeypatch.setattr(m5, "urlopen", _explode)
    monkeypatch.setattr(m5, "run_wechat_step", _explode)
    env = {"M5_LIVE_APPROVED": "1", "M5_LIVE_STEPS": "deepseek"}

    assert m5.live_step_enabled("deepseek", env) is True
    assert m5.live_step_enabled("wechat", env) is False

    wechat = m5.run_step("wechat", tmp_path, "run-subset", env=env)
    assert wechat == {"step": "wechat", "skipped": True, "reason": "M5_LIVE_STEPS excludes wechat"}

    # Unset keeps both probes selected (default behavior unchanged).
    default_env = {"M5_LIVE_APPROVED": "1"}
    assert m5.live_step_enabled("deepseek", default_env) is True
    assert m5.live_step_enabled("wechat", default_env) is True


# ── DeepSeek durable artifact ─────────────────────────────────────────────


def _artifact_path(tmp_path: Path, run_id: str) -> Path:
    return tmp_path / ".verifykit" / "data" / "m5" / run_id / "deepseek" / "result.json"


def test_deepseek_writes_redacted_artifact_on_success(tmp_path, deepseek_key):
    transport = _transport(_deepseek_body(_ok_payload()))
    m5.run_deepseek_step(tmp_path, "run-artifact-ok", transport=transport)

    path = _artifact_path(tmp_path, "run-artifact-ok")
    assert path.exists()
    artifact = json.loads(path.read_text(encoding="utf-8"))

    assert set(artifact) == set(m5.DEEPSEEK_ARTIFACT_FIELDS)
    assert "ok" not in artifact
    assert artifact == {
        "step": "deepseek",
        "attempted": True,
        "request_model_id": "deepseek-flash",
        "response_model_id": "deepseek-chat",
        "auth_class": "ok",
        "http_status": 200,
        "content_non_empty": True,
        "elapsed_ms": artifact["elapsed_ms"],
        "usage": {"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13},
        "cost": "unknown",
        "provider_request_id": "chatcmpl-provider-1",
        "error_class": None,
    }
    assert isinstance(artifact["elapsed_ms"], int) and artifact["elapsed_ms"] >= 0


def test_deepseek_writes_artifact_on_failure(tmp_path, deepseek_key):
    calls = []

    def _open(request, timeout=None):
        calls.append(request)
        raise TimeoutError("timed out")

    result = m5.run_deepseek_step(tmp_path, "run-artifact-timeout", transport=_open)
    assert result["error_class"] == "timeout"
    assert len(calls) == 1

    artifact = json.loads(_artifact_path(tmp_path, "run-artifact-timeout").read_text(encoding="utf-8"))
    assert set(artifact) == set(m5.DEEPSEEK_ARTIFACT_FIELDS)
    assert artifact["attempted"] is True
    assert artifact["auth_class"] == "timeout"
    assert artifact["http_status"] is None
    assert artifact["content_non_empty"] is False
    assert artifact["usage"] == "unknown"
    assert artifact["cost"] == "unknown"
    assert artifact["provider_request_id"] is None
    assert artifact["error_class"] == "timeout"


def test_deepseek_artifact_is_redacted(tmp_path, monkeypatch):
    secret = "sk-artifact-super-secret"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)

    def _open(request, timeout=None):
        raise urllib.error.HTTPError(
            "https://api.deepseek.com/chat/completions",
            500,
            "Server Error",
            {},
            io.BytesIO(b'{"error":{"message":"raw provider error body"}}'),
        )

    m5.run_deepseek_step(tmp_path, "run-artifact-redact", transport=_open)
    blob = _artifact_path(tmp_path, "run-artifact-redact").read_text(encoding="utf-8")

    assert secret not in blob
    assert m5.DEEPSEEK_PROMPT not in blob
    assert "raw provider error body" not in blob
    assert "Authorization" not in blob
    assert "Bearer" not in blob
    assert "messages" not in blob


def test_deepseek_artifact_not_clobbered_by_a_second_attempt(tmp_path, deepseek_key):
    first_transport = _transport(_deepseek_body(_ok_payload()))
    m5.run_deepseek_step(tmp_path, "run-artifact-once", transport=first_transport)
    before = _artifact_path(tmp_path, "run-artifact-once").read_text(encoding="utf-8")

    second_transport = _transport(_deepseek_body(_ok_payload(model="deepseek-reasoner", id="chatcmpl-2")))
    second = m5.run_deepseek_step(tmp_path, "run-artifact-once", transport=second_transport)
    assert second["error_class"] == "already_attempted"
    assert len(second_transport.calls) == 0
    assert _artifact_path(tmp_path, "run-artifact-once").read_text(encoding="utf-8") == before


def test_cli_runs_standalone_and_stays_offline(tmp_path):
    """The adapter must be importable as a standalone script (repo root on path).

    With the gate on but every credential blanked, both probes must short-circuit
    to a classified `not_configured` without any network request.
    """
    env = dict(os.environ)
    env.update(
        {
            "M5_LIVE_APPROVED": "1",
            "DEEPSEEK_API_KEY": "",
            "LLM_API_KEY": "",
            "WECHAT_APP_ID": "",
            "WECHAT_APP_SECRET": "",
        }
    )
    script = BRIDGE_DIR / "m5_acceptance.py"
    for step, expected in (("deepseek", "not_configured"), ("wechat", "wechat_not_configured")):
        proc = subprocess.run(
            [
                sys.executable,
                str(script),
                "--step",
                step,
                "--repo-root",
                str(tmp_path),
                "--run-id",
                f"cli-{step}",
            ],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(REPO_ROOT),
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        assert payload["ok"] is False
        assert payload["error_class"] == expected, payload


# ── DeepSeek probe ────────────────────────────────────────────────────────


def test_deepseek_request_body_is_bounded():
    body = m5.deepseek_request_body("deepseek-flash")
    assert body["max_tokens"] == 128
    assert body["stream"] is False
    assert len(body["messages"]) == 1
    assert body["messages"][0]["role"] == "user"
    assert len(body["messages"][0]["content"]) < 200


def test_deepseek_records_provider_metadata_once(tmp_path, deepseek_key):
    transport = _transport(_deepseek_body(_ok_payload()))
    first = m5.run_deepseek_step(tmp_path, "run-1", transport=transport)

    assert first["ok"] is True
    assert first["attempted"] is True
    assert first["http_status"] == 200
    assert first["auth_class"] == "ok"
    assert first["response_model_id"] == "deepseek-chat"
    assert first["provider_request_id"] == "chatcmpl-provider-1"
    assert first["content_non_empty"] is True
    assert first["usage"] == {"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13}
    assert transport.calls[0]["timeout"] == m5.DEEPSEEK_TIMEOUT_SECONDS

    second = m5.run_deepseek_step(tmp_path, "run-1", transport=transport)
    assert second["attempted"] is False
    assert second["ok"] is False
    assert second["error_class"] == "already_attempted"
    assert len(transport.calls) == 1


def test_deepseek_response_read_is_capped(tmp_path, deepseek_key):
    captured = {}

    def _open(request, timeout=None):
        response = _FakeResponse(_deepseek_body(_ok_payload()))
        captured["response"] = response
        return response

    m5.run_deepseek_step(tmp_path, "run-cap", transport=_open)
    assert captured["response"].read_limit == m5.DEEPSEEK_MAX_RESPONSE_BYTES


def test_deepseek_uses_header_request_id_when_body_has_none(tmp_path, deepseek_key):
    payload = _ok_payload()
    payload.pop("id")
    transport = _transport(_deepseek_body(payload), headers={"x-request-id": "req-abc-9"})
    result = m5.run_deepseek_step(tmp_path, "run-hdr", transport=transport)
    assert result["provider_request_id"] == "req-abc-9"


def test_deepseek_never_synthesizes_request_id(tmp_path, deepseek_key):
    payload = _ok_payload()
    payload.pop("id")
    transport = _transport(_deepseek_body(payload))
    result = m5.run_deepseek_step(tmp_path, "run-noid", transport=transport)
    assert result["ok"] is True
    assert result["provider_request_id"] is None


def test_deepseek_empty_content_is_a_classified_failure(tmp_path, deepseek_key):
    payload = _ok_payload(choices=[{"message": {"content": "   "}}])
    transport = _transport(_deepseek_body(payload))
    result = m5.run_deepseek_step(tmp_path, "run-empty", transport=transport)
    assert result["ok"] is False
    assert result["error_class"] == "empty_content"
    assert result["content_non_empty"] is False
    assert result["provider_request_id"] == "chatcmpl-provider-1"


def test_deepseek_timeout_is_not_retried(tmp_path, deepseek_key):
    calls = []

    def _open(request, timeout=None):
        calls.append(request)
        raise TimeoutError("timed out")

    result = m5.run_deepseek_step(tmp_path, "run-timeout", transport=_open)
    assert result["ok"] is False
    assert result["error_class"] == "timeout"
    assert result["attempted"] is True
    assert len(calls) == 1


def test_deepseek_auth_error_is_classified(tmp_path, deepseek_key):
    def _open(request, timeout=None):
        raise urllib.error.HTTPError(
            "https://api.deepseek.com/chat/completions",
            401,
            "Unauthorized",
            {},
            io.BytesIO(b'{"error":{"message":"invalid key"}}'),
        )

    result = m5.run_deepseek_step(tmp_path, "run-401", transport=_open)
    assert result["ok"] is False
    assert result["error_class"] == "auth_rejected"
    assert result["http_status"] == 401


def test_deepseek_missing_credential_makes_no_request(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    def _explode(*_args, **_kwargs):
        raise AssertionError("no credential means no request")

    result = m5.run_deepseek_step(tmp_path, "run-nokey", transport=_explode, env={})
    assert result["ok"] is False
    assert result["error_class"] == "not_configured"
    assert result["attempted"] is True


def test_deepseek_result_leaks_no_secret_prompt_or_body(tmp_path, monkeypatch):
    secret = "sk-super-secret-value"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)

    def _open(request, timeout=None):
        raise urllib.error.HTTPError(
            "https://api.deepseek.com/chat/completions",
            500,
            "Server Error",
            {},
            io.BytesIO(b'{"error":{"message":"raw provider body"}}'),
        )

    result = m5.run_deepseek_step(tmp_path, "run-redact", transport=_open)
    blob = json.dumps(result, ensure_ascii=False)
    assert secret not in blob
    assert m5.DEEPSEEK_PROMPT not in blob
    assert "raw provider body" not in blob
    assert set(result) == set(m5.DEEPSEEK_RESULT_FIELDS)
    assert "content" not in result
    assert "prompt" not in result


# ── WeChat probe ──────────────────────────────────────────────────────────


def test_wechat_isolated_queue_creates_exactly_one_draft(tmp_path):
    pub = _FakePublisher()
    before_pending = publish_queue.PENDING
    result = m5.run_wechat_step(
        tmp_path, "run-w", article_src=_article(tmp_path), publisher_factory=lambda: pub
    )

    assert result["ok"] is True, result
    assert result["attempted"] is True
    assert result["draft_created"] is True
    assert result["media_id"] == "MEDIA-123"
    assert result["media_id_matches"] is True
    assert result["published"] is False
    assert result["done_records"] == 1
    assert pub.calls == {"parse": 1, "create": 1}

    queue_dir = Path(result["isolated_workspace"]) / "queue"
    done = publish_queue.read_records_unlocked(queue_dir / "done.jsonl")
    assert len(done) == 1
    assert done[0]["media_id"] == "MEDIA-123"
    assert done[0]["published"] is False
    pending = publish_queue.read_records_unlocked(queue_dir / "pending.jsonl")
    assert pending == []

    # The live queue module globals are restored after the isolated run.
    assert publish_queue.PENDING == before_pending


def test_wechat_reuses_permanent_cover_and_skips_image_upload(tmp_path):
    pub = _FakePublisher()
    m5.run_wechat_step(
        tmp_path, "run-cover", article_src=_article(tmp_path), publisher_factory=lambda: pub
    )
    assert pub.thumb in wechat_publish.PERMANENT_COVERS.values()
    assert pub.thumb == m5.cover_media_id(ARTICLE_TITLE)


def test_wechat_second_run_is_blocked(tmp_path):
    pub = _FakePublisher()
    article = _article(tmp_path)
    first = m5.run_wechat_step(
        tmp_path, "run-once", article_src=article, publisher_factory=lambda: pub
    )
    second = m5.run_wechat_step(
        tmp_path, "run-once", article_src=article, publisher_factory=lambda: pub
    )
    assert first["ok"] is True
    assert second["ok"] is False
    assert second["error_class"] == "already_attempted"
    assert pub.calls["create"] == 1


def test_wechat_failure_leaves_no_done_record(tmp_path):
    pub = _FakePublisher(fail=True)
    result = m5.run_wechat_step(
        tmp_path, "run-fail", article_src=_article(tmp_path), publisher_factory=lambda: pub
    )
    assert result["ok"] is False
    assert result["error_class"] == "publish_failed"
    assert result["done_records"] == 0
    assert result["media_id"] is None
    assert result["draft_created"] is False
    assert set(result) == set(m5.WECHAT_RESULT_FIELDS)


def test_wechat_missing_credentials_do_not_claim_the_attempt(tmp_path, monkeypatch):
    monkeypatch.setattr(wechat_publish, "WECHAT_APP_ID", "")
    monkeypatch.setattr(wechat_publish, "WECHAT_APP_SECRET", "")
    result = m5.run_wechat_step(tmp_path, "run-nocreds", article_src=_article(tmp_path))
    assert result["ok"] is False
    assert result["error_class"] == "wechat_not_configured"
    assert result["attempted"] is False
    marker = tmp_path / ".verifykit" / "data" / "m5" / "run-nocreds" / "wechat" / "draft.attempted"
    assert not marker.exists()


def test_wechat_missing_test_article_is_reported(tmp_path):
    result = m5.run_wechat_step(tmp_path, "run-noarticle", article_src=tmp_path / "missing.md")
    assert result["ok"] is False
    assert result["error_class"] == "test_article_missing"
    assert result["attempted"] is False
