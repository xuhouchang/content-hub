#!/usr/bin/env python3
"""Round-3 verification gates (R1/R2) — runnable with stdlib unittest only.

Run from the repo root:
    python tests/test_round3_fixes.py

No third-party deps, no network, no git-history rewrites, no WeChat sends.
"""
import os
import sys
import json
import time
import io
import contextlib
import tempfile
from pathlib import Path
from unittest import TestCase, mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import lib.llm as llm
import write_article as wa
import publish_worker as pw


def _read(p):
    """Safe read: return '' if the queue file was never created."""
    return p.read_text(encoding="utf-8") if p.exists() else ""


class TestR1DeadlineAutoDegrade(TestCase):
    def test_call_model_aborts_on_past_deadline(self):
        """R1: a past LLM_DEADLINE must make call_model return None and log it."""
        with mock.patch.object(llm, "LLM_DEADLINE", 1000.0):  # epoch 1970, far past
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                result = llm.call_model(
                    [{"role": "user", "content": "hi"}], max_tokens=10
                )
            self.assertIsNone(result)
            self.assertIn("deadline exceeded", buf.getvalue())

    def test_run_image_search_skips_when_budget_exhausted(self):
        """R1: when IMAGE_SEARCH_DEADLINE is nearly exhausted, run_image_search
        must skip (return False) WITHOUT spawning the image_search subprocess."""
        with mock.patch.dict(
            os.environ, {"IMAGE_SEARCH_DEADLINE": str(time.time() - 100)}
        ):
            calls = []

            def fake_run(*a, **k):
                calls.append((a, k))

                class R:
                    returncode = 0
                    stdout = ""
                    stderr = ""

                return R()

            with mock.patch("subprocess.run", fake_run):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    result = wa.run_image_search("test query", "/tmp/r3_nope", 1)
        self.assertFalse(result)
        self.assertEqual(
            calls, [], "subprocess.run must NOT be invoked when budget exhausted"
        )
        self.assertIn("budget nearly exhausted", buf.getvalue())


class TestR2SelfHeal(TestCase):
    def _setup(self, tmp_path, records, age_seconds):
        pw.QUEUE_DIR = tmp_path
        pw.PROCESSING = tmp_path / "processing.jsonl"
        pw.PENDING = tmp_path / "pending.jsonl"
        pw.DONE = tmp_path / "done.jsonl"
        pw.FAILED = tmp_path / "failed.jsonl"
        text = (
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n"
        )
        pw.PROCESSING.write_text(text, encoding="utf-8")
        if age_seconds:
            old = time.time() - age_seconds
            os.utime(pw.PROCESSING, (old, old))

    def test_heal_media_id_to_done(self):
        tmp = Path(tempfile.mkdtemp())
        self._setup(
            tmp,
            [
                {"output_dir": "/tmp/art/a", "media_id": "MEDIA1234567890"},
                {"output_dir": "/tmp/art/b"},
            ],
            age_seconds=20 * 60,  # stale > 15min
        )
        pw._check_processing_leftovers()
        done = [
            json.loads(l)
            for l in _read(pw.DONE).splitlines()
            if l.strip()
        ]
        pending = [
            json.loads(l)
            for l in _read(pw.PENDING).splitlines()
            if l.strip()
        ]
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["media_id"], "MEDIA1234567890")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["output_dir"], "/tmp/art/b")
        self.assertEqual(pw.PROCESSING.read_text(encoding="utf-8").strip(), "")

    def test_heal_no_media_id_to_pending(self):
        tmp = Path(tempfile.mkdtemp())
        self._setup(tmp, [{"output_dir": "/tmp/art/c"}], age_seconds=20 * 60)
        pw._check_processing_leftovers()
        done = _read(pw.DONE).strip()
        pending = [
            json.loads(l)
            for l in _read(pw.PENDING).splitlines()
            if l.strip()
        ]
        self.assertEqual(done, "")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["output_dir"], "/tmp/art/c")

    def test_fresh_not_healed(self):
        tmp = Path(tempfile.mkdtemp())
        self._setup(
            tmp, [{"output_dir": "/tmp/art/d", "media_id": "M"}], age_seconds=0
        )
        pw._check_processing_leftovers()
        self.assertNotEqual(pw.PROCESSING.read_text(encoding="utf-8").strip(), "")
        self.assertEqual(_read(pw.PENDING).strip(), "")
        self.assertEqual(_read(pw.DONE).strip(), "")


if __name__ == "__main__":
    import unittest

    unittest.main(verbosity=2)
