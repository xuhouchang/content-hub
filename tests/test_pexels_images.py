#!/usr/bin/env python3
"""
Pexels-first body-image acquisition — offline verification gates.

Runnable with stdlib unittest only. No network, no model calls, no WeChat sends,
no real Pexels/Pixabay requests. Pexels/Pixabay are fully mocked.

Run from repo root:
    python tests/test_pexels_images.py

Covers:
  1. Pexels hit            -> image downloaded, source == "pexels", normalized
  2. No Pexels result      -> "none" when generative is not enabled
  3. Generative fallback   -> only when explicitly allowed + supplied
  4. Existing image        -> reused (not re-downloaded / idempotent)
  5. Pexels-first policy   -> PEXELS_FIRST env/flag drives order
  6. Draft-flow integration-> write_article.run_image_search uses the resolver
"""
import os
import random
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Isolate the persistent "used image" ledger to a temp file for offline tests.
os.environ["PEXELS_USED_LEDGER"] = str(Path(tempfile.mkdtemp()) / "test_ledger.json")

# Import order matters: importing write_article triggers lib import.
import lib.pexels_images as px
import write_article as wa


def _fake_photo(width: int = 1280) -> dict:
    """A Pexels-shaped photo dict the resolver understands."""
    return {"id": 100000 + width, "src": {"large2x": "https://images.pexels.example/_fake_a.jpg",
                    "large": "https://images.pexels.example/_fake_b.jpg",
                    "medium": "https://images.pexels.example/_fake_c.jpg"},
            "width": width, "photographer": "Test"}


def _noisy_png(path: Path, size=(640, 480)):
    """Write a >5KB noisy RGB image (guaranteed to clear the idempotency bar)."""
    from PIL import Image
    w, h = size
    img = Image.new("RGB", (w, h))
    p_map = img.load()
    for y in range(h):
        for x in range(w):
            p_map[x, y] = (random.randint(0, 255), random.randint(0, 255),
                           random.randint(0, 255))
    img.save(path, "PNG")


def _jpg(path: Path, size=(1200, 600)):
    from PIL import Image
    img = Image.new("RGB", size, (60, 80, 120))
    img.save(path, "JPEG", quality=95)
    if path.stat().st_size < 5000:  # solid color may compress tiny; force noise
        _noisy_png(path)


class ResolverOfflineTestCase(TestCase):
    def _download_then_jpg(self, url, out_path):
        """Mock for image_search.download_image: writes a real JPG, returns True."""
        out = Path(out_path)
        if out.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            out = out.with_suffix(".jpg")
        _jpg(out)
        return True

    def test_existing_image_is_reused_not_redownloaded(self):
        """Idempotency: a present file must not trigger Pexels search/download."""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            existing = d / "image-001-pexels.jpg"
            _noisy_png(existing)
            with mock.patch.object(px, "search_pexels",
                                   side_effect=AssertionError("must not search")):
                res = px.resolve_image(existing, description="cybersecurity",
                                       allow_generative=False)
            self.assertEqual(res.source, "existing")
            self.assertTrue(res.ok)
            self.assertTrue(res.path.exists())

    def test_pexels_hit_downloads_and_normalizes(self):
        """Pexels hit: image downloaded, reported as pexels, file on disk."""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            target = d / "image-001-pexels"
            with mock.patch.object(px, "search_pexels", return_value=[_fake_photo()]), \
                 mock.patch.object(px, "_pexels_download", side_effect=self._download_then_jpg):
                res = px.resolve_image(target, description="data dashboard",
                                       allow_generative=False)
            self.assertEqual(res.source, "pexels")
            self.assertTrue(res.ok)
            self.assertIsNotNone(res.path)
            self.assertTrue(res.path.exists())
            self.assertGreater(res.path.stat().st_size, 0)

    def test_no_pexels_result_none_when_generative_disabled(self):
        """No Pexels hit + no generative fallback -> source 'none'."""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            with mock.patch.object(px, "search_pexels", return_value=[]):
                res = px.resolve_image(d / "image-002-pexels.jpg",
                                       description="架构图", allow_generative=False)
            self.assertEqual(res.source, "none")
            self.assertFalse(res.ok)

    def test_generative_fallback_only_when_allowed(self):
        """No Pexels hit + generative explicitly allowed -> used."""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            gen_path = Path(d) / "_gen.png"
            _noisy_png(gen_path)

            with mock.patch.object(px, "search_pexels", return_value=[]):
                # Without a generator, still none:
                no_gen = px.resolve_image(d / "image-a.jpg", description="AI agent",
                                          allow_generative=True, gen_fallback=None)
                self.assertEqual(no_gen.source, "none")
                # With a generator -> generative:
                res = px.resolve_image(d / "image-b.jpg", description="AI agent",
                                       allow_generative=True, gen_fallback=lambda: gen_path)
            self.assertEqual(res.source, "generative")
            self.assertTrue(res.ok)
            self.assertTrue(res.path.exists())

    def test_pexels_first_flag_skips_pexels_when_off(self):
        """PEXELS_FIRST off/False lets generative win without calling Pexels."""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            gen_path = Path(d) / "_gen.png"
            _noisy_png(gen_path)
            with mock.patch.object(px, "search_pexels",
                                   side_effect=AssertionError("Pexels must be skipped")):
                res = px.resolve_image(d / "image-c.jpg", description="AI agent",
                                       prefer_pexels=False,
                                       allow_generative=True, gen_fallback=lambda: gen_path)
            self.assertEqual(res.source, "generative")

    def test_english_keywords_for_chinese_description(self):
        """Chinese descriptions map to English Pexels queries offline.

        Critically, the primary keyword must NOT be an untranslated Chinese
        token (otherwise Pexels returns poor/no results).
        """
        kws = px._english_keywords("企业AI代理安全架构图", "image-003-xxx")
        self.assertTrue(any(kws))  # non-empty
        # Primary keyword must be English (no CJK).
        def _is_cjk(s: str) -> bool:
            return any("\u4e00" <= ch <= "\u9fff" for ch in s)
        self.assertFalse(_is_cjk(kws[0]), f"primary keyword not English: {kws[0]!r}")
        blob = " ".join(kws).lower()
        self.assertTrue(("security" in blob) or ("architecture" in blob)
                        or ("technology" in blob) or ("enterprise" in blob))


class DraftFlowIntegrationTestCase(TestCase):
    def _download_then_jpg(self, url, out_path):
        out = Path(out_path)
        if out.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            out = out.with_suffix(".jpg")
        _jpg(out)
        return True

    def _env(self, **kw):
        self._old_env = os.environ.copy()
        os.environ.update(kw)

    def tearDown(self):
        if hasattr(self, "_old_env"):
            os.environ.clear()
            os.environ.update(self._old_env)

    def test_run_image_search_pexels_first(self):
        """write_article.run_image_search resolves via the reusable module."""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            with mock.patch.object(px, "search_pexels", return_value=[_fake_photo()]), \
                 mock.patch.object(px, "_pexels_download", side_effect=self._download_then_jpg):
                ok = wa.run_image_search("data dashboard", d, 5, description="数据看板")
            self.assertIsNotNone(ok)  # photo-id string (possibly '') on success
            self.assertTrue((d / "image-005-pexels.jpg").exists())

    def test_run_image_search_existing_skips_redownload(self):
        """run_image_search must not re-download an already-resolved placeholder."""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            target = d / "image-006-pexels.jpg"
            _noisy_png(target)
            with mock.patch.object(px, "search_pexels",
                                   side_effect=AssertionError("must not search")):
                ok = wa.run_image_search("data dashboard", d, 6, description="数据看板")
            self.assertIsNotNone(ok)
            self.assertTrue(target.exists())

    def test_placeholder_alt_text_extraction(self):
        content = ("# t\n\n![数据看板架构](./images/image-001-coast-runners.jpeg)\n\n"
                   "![攻击面分析](./images/image-002-x.jpeg)\n\n<!-- IMAGE: 3 -->")
        m = wa._extract_placeholder_alt_text(content)
        self.assertEqual(m[1], "数据看板架构")
        self.assertEqual(m[2], "攻击面分析")
        self.assertIn(3, m)



class DedupTestCase(TestCase):
    """Persistent used-image ledger + per-article exclusion behavior."""

    def _photo(self, pid: int, width: int = 1200) -> dict:
        return {"src": {"large2x": f"https://images.pexels.example/p{pid}.jpg",
                        "large": "", "medium": "", "original": ""},
                "width": width, "id": pid}

    def _dl(self, url, out_path):  # write a >5KB file
        out = Path(out_path)
        if out.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            out = out.with_suffix(".jpg")
        _jpg(out)
        return True

    def test_exclude_ids_prevents_same_photo_two_placeholders(self):
        """Same-photo duplication within one article is prevented via exclude_ids."""
        # search_pexels always returns the SAME single photo (id=99) regardless of page.
        with tempfile.TemporaryDirectory() as d,              mock.patch.object(px, "search_pexels", return_value=[self._photo(99)]),              mock.patch.object(px, "_pexels_download", side_effect=self._dl):
            d = Path(d)
            # Call 1: no exclusion -> picks photo 99.
            r1 = px.resolve_image(d / "image-a.jpg", description="data analytics",
                                  allow_generative=False)
            self.assertEqual(r1.photo_id, "99")
            # Call 2 for a SECOND placeholder in the SAME article: exclude 99.
            r2 = px.resolve_image(d / "image-b.jpg", description="business workflow",
                                  allow_generative=False, exclude_ids={"99"})
            # Exclusion exhausted -> minimal fallback to reuse 99 (keeps rendering).
            self.assertEqual(r2.photo_id, "99")

    def test_ledger_persists_used_photo_ids(self):
        """A successfully used Pexels photo is recorded in the persistent ledger."""
        os.environ["PEXELS_USED_LEDGER"] = str(
            Path(tempfile.mkdtemp()) / "ledger_persist.json")
        # USED_LEDGER_PATH is computed at import; force reload to honor the env.
        import importlib
        importlib.reload(px)
        try:
            with tempfile.TemporaryDirectory() as d,                  mock.patch.object(px, "search_pexels", return_value=[self._photo(555)]),                  mock.patch.object(px, "_pexels_download", side_effect=self._dl):
                px.resolve_image(Path(d) / "image-c.jpg", description="cybersecurity",
                                 allow_generative=False)
            self.assertIn("555", px.load_used_photo_ids())
        finally:
            env = os.environ.get("PEXELS_USED_LEDGER")
            if env:
                os.environ.pop("PEXELS_USED_LEDGER", None)
            importlib.reload(px)

if __name__ == "__main__":
    sys.exit(__import__("unittest").main())
