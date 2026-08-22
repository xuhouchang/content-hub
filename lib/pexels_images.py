#!/usr/bin/env python3
"""
Reusable "Pexels first" image acquisition for WeChat article body illustrations.

This is the authoritative, importable entry point for grabbing a body/reinforcement
image. It enforces the Pexels-first policy rather than relying on a memory
convention: by default it searches Pexels (via the shared `image_search` module),
and only falls back to a generator callable when either:

  * the caller explicitly opts-in (`allow_generative=True`), or
  * Pexels had no suitable result AND a generator is supplied (`gen_fallback`).

It also refuses to re-download an image that already exists on disk (idempotency),
and can post-process the downloaded photo (crop/resize to a target size + normalize
to JPEG) using Pillow.

Design notes
------------
* Pure stdlib + PIL. No third-party network deps beyond `image_search`, which
  already uses urllib. Importing this module does NOT hit the network; only the
  fetch functions do.
* All functions return structured results so the caller (agent or pipeline) can
  report clearly and decide on further fallback.
* `image_search.search_pexels` / `image_search.download_image` are reused so there
  is a single source of truth for Pexels API access and download/placeholder rules.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable, Optional

from PIL import Image

# Reuse the canonical Pexels search + download from image_search.py so API keys,
# user-agent, size limits and placeholder self-degradation stay consistent.
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from image_search import search_pexels, download_image as _pexels_download


# Env-driven default for the Pexels-first policy. When truthy (default), body
# images resolve from Pexels before any generative fallback. Set to "0"/"false"
# to let generative images win outright.
PEXELS_FIRST = os.environ.get("PEXELS_FIRST", "1").lower() not in {"0", "false", "no", ""}

# Allowed output image types for post-processing.
ALLOWED_OUTPUT_TYPES = {".jpg", ".jpeg", ".png", ".webp"}

# Default target long edge (px) applied during post-process.
DEFAULT_LONG_EDGE = 1280


# ════════════════════════════════════════════════════════════════
#  Keyword generation
# ════════════════════════════════════════════════════════════════
def _english_keywords(description: str, filename: str = "") -> list[str]:
    """Turn a Chinese/English human description into Pexels search keywords.

    Uses a local mapping first (fast, deterministic, offline), then falls back
    to the LLM-assisted keyword generator used for covers so long descriptions
    still yield decent stock-photo queries. Never raises.
    """
    keywords: list[str] = []

    stem = Path(filename).stem if filename else ""
    stem_clean = re.sub(r"^\d+-", "", stem)
    stem_clean = re.sub(r"image-\d+-", "", stem_clean)
    stem_clean = stem_clean.replace("-", " ").replace("_", " ").strip()

    # Heuristic topic → English query (mirrors fill_article_images mappings so a
    # Chinese alt/description still finds relevant Pexels photos offline). These
    # MUST outrank a possibly-Chinese filename stem so the primary query is a
    # useful English Pexels search string.
    _map = {
        "架构": "technology system architecture", "安全": "cybersecurity network security",
        "数据": "data analytics business dashboard", "对比": "comparison analysis technology",
        "攻击面": "cyber attack network security", "风控": "risk management compliance",
        "效率": "productivity workplace efficiency", "流程": "business workflow automation",
        "协作": "team collaboration meeting", "AI": "artificial intelligence enterprise",
        "agent": "AI agent automation", "机器人": "AI robot assistant technology",
        "治理": "corporate governance compliance", "审计": "audit review documentation",
        "架构图": "network infrastructure server room", "图": "technology visualization abstract",
    }
    _text = description + " " + stem_clean
    for cn, en in _map.items():
        if cn.lower() in _text.lower():
            keywords.append(en)
    # Append the filename stem only if it looks like an English keyword (helps
    # attach a real English filename stem, but avoids an untranslated Chinese
    # token becoming the primary query).
    if stem_clean and stem_clean.lower() not in ("pexels", "placeholder") \
            and not any(ch >= "\u4e00" and ch <= "\u9fff" for ch in stem_clean):
        keywords.append(stem_clean)

    if keywords:
        return keywords[:3]


    # Offline fallback vocabulary when nothing mapped and no useful filename—so we
    # never depend on the LLM just to build a search query.
    return ["enterprise technology abstract", "professional workspace", "digital business"] + (
        _english_keywords_llm(description) if description else []
    )


def _english_keywords_llm(description: str) -> list[str]:
    """Optional LLM keyword enhancement for long descriptions. Best-effort."""
    try:
        from generate_cover import generate_search_keywords
        return generate_search_keywords("配图", description)
    except Exception:
        return []


# ════════════════════════════════════════════════════════════════
#  Post-processing (size / format normalization)
# ════════════════════════════════════════════════════════════════
def _postprocess(
    src_path: Path,
    dest_path: Path,
    *,
    long_edge: Optional[int] = DEFAULT_LONG_EDGE,
    force_jpeg: bool = True,
) -> Path:
    """Normalize an image to a target long edge and (optionally) JPEG.

    Returns the final path. Uses center-crop when the source is wider than the
    current file's ratio requires? No—we preserve aspect ratio, only resize the
    long edge, mirroring generate_cover's quality bar without forcing a ratio.
    """
    dest_path = dest_path if dest_path.suffix.lower() in ALLOWED_OUTPUT_TYPES else dest_path.with_suffix(".jpg")
    if dest_path.suffix.lower() not in {".jpg", ".jpeg"} and not force_jpeg:
        pass
    out = dest_path.with_suffix(".jpg" if force_jpeg else dest_path.suffix)

    img = Image.open(src_path)
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    w, h = img.size
    long_edge = long_edge or DEFAULT_LONG_EDGE
    if max(w, h) > long_edge:
        scale = long_edge / float(max(w, h))
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    img.save(out, "JPEG", quality=90)
    return out


# ════════════════════════════════════════════════════════════════
#  Public API
# ════════════════════════════════════════════════════════════════
class PexelsResult:
    """Result of a resolve_* call."""

    __slots__ = ("ok", "path", "source", "reason", "query")

    def __init__(self, ok: bool, path: Optional[Path], source: str, reason: str, query: str = ""):
        self.ok = ok            # True → a usable image file now exists at `path`
        self.path = path        # final file path (None when not written)
        self.source = source    # "existing" | "pexels" | "generative" | "placeholder" | "none"
        self.reason = reason    # human-readable explanation
        self.query = query      # the Pexels query actually used ("" if none)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<PexelsResult ok={self.ok} source={self.source} path={self.path} reason={self.reason!r} query={self.query!r}>"


def resolve_image(
    out_path: str | Path,
    description: str = "",
    *,
    query: Optional[str] = None,
    filename_hint: str = "",
    output_dir: Optional[str | Path] = None,
    allow_generative: Optional[bool] = None,
    gen_fallback: Optional[Callable[..., Optional[str | Path]]] = None,
    prefer_pexels: Optional[bool] = None,
    long_edge: Optional[int] = DEFAULT_LONG_EDGE,
    per_page: int = 5,
    min_width: int = 800,
) -> PexelsResult:
    """Resolve a body illustration for `out_path` with a Pexels-first policy.

    Resolution order (Pexels first):
      1. If a non-trivial file already exists at `out_path`, reuse it (idempotent).
      2. If Pexels is enabled (default), search via the supplied `query` (or
         derive keywords from `description`), download the best photo, and
         optionally post-process to `long_edge`.
      3. If Pexels found nothing AND `allow_generative` is true AND `gen_fallback`
         is provided, call the generator to synthesize an image.
      4. Last resort: leave as-is / report no source (caller may use placeholder).

    Args:
        out_path:      destination file (e.g. ./images/image-002.jpg)
        description:   Chinese/English alt text used to derive a query when `query` is None
        query:         explicit Pexels search string (overrides derivation)
        filename_hint: original placeholder filename (e.g. "image-002-xxx") for query hints
        output_dir:    if given, `out_path` is resolved relative to it
        allow_generative: explicitly allow generative fallback (default: PEXELS_FIRST is
                          off, i.e. True when PEXELS_FIRST=0). When None, follows policy.
        gen_fallback:  callable() -> path to a generated image file, OR None.
        prefer_pexels: force Pexels even when generative is allowed. Default follows
                       PEXELS_FIRST.
        long_edge:     post-process target long edge (px). None disables resize.
        per_page:      Pexels page size for the search.
        min_width:     minimum source width (px) to accept a Pexels photo.
    """
    out_path = Path(out_path)
    if output_dir is not None:
        out_path = Path(output_dir) / out_path

    # 1) Idempotency: reuse an existing non-trivial image.
    if _existing_image(out_path):
        return PexelsResult(True, out_path, "existing",
                            "image already present, not re-downloaded")

    prefer_pexels = PEXELS_FIRST if prefer_pexels is None else bool(prefer_pexels)

    # 2) Pexels first.
    if prefer_pexels:
        q = query or (_build_query(description, filename_hint))
        best = _search_download(out_path, q, per_page=per_page, min_width=min_width)
        if best:
            if long_edge:
                try:
                    final = _postprocess(best, out_path, long_edge=long_edge)
                    return PexelsResult(True, final, "pexels",
                                        "downloaded from Pexels and normalized", q)
                except Exception as e:  # keep the raw download if normalize fails
                    return PexelsResult(True, best, "pexels",
                                        f"downloaded from Pexels (normalize skipped: {e})", q)
            return PexelsResult(True, best, "pexels", "downloaded from Pexels", q)

    # 3) Generative fallback only if explicitly allowed.
    allow_generative = (not prefer_pexels) if allow_generative is None else bool(allow_generative)
    if allow_generative and gen_fallback is not None:
        try:
            gen_path = gen_fallback()
            if gen_path:
                gen_path = Path(gen_path)
                if out_path.suffix.lower() in ALLOWED_OUTPUT_TYPES and gen_path != out_path:
                    import shutil
                    shutil.copyfile(gen_path, out_path)
                return PexelsResult(True, out_path, "generative",
                                    "Pexels had no hit; used generative fallback",
                                    query if query else (_build_query(description, filename_hint)))
        except Exception as e:
            return PexelsResult(False, None, "none",
                                f"generative fallback failed: {e}",
                                query or "")

    # 4) Nothing usable.
    return PexelsResult(False, None, "none",
                        "no existing image and Pexels returned nothing"
                        + ("" if allow_generative or gen_fallback is None
                           else " (generative fallback not enabled)"),
                        query if query else (_build_query(description, filename_hint)))


# ════════════════════════════════════════════════════════════════
#  Internals
# ════════════════════════════════════════════════════════════════
def _existing_image(path: Path) -> bool:
    """A usable image already exists (exists + non-trivial size + image ext)."""
    if not path.exists():
        return False
    try:
        if path.stat().st_size < 5000:
            return False
    except OSError:
        return False
    if path.suffix.lower() not in ALLOWED_OUTPUT_TYPES:
        return False
    return True


def _build_query(description: str, filename_hint: str = "") -> str:
    """First keyword from the heuristic set, else a generic enterprise query."""
    kws = _english_keywords(description, filename_hint)
    return kws[0] if kws else "enterprise business technology"


def _search_download(
    out_path: Path,
    query: str,
    *,
    per_page: int = 5,
    min_width: int = 800,
) -> Optional[Path]:
    """Search Pexels and download the best landscape photo to `out_path`.

    Returns the downloaded file path on success, else None. Does NOT post-process.
    Uses image_search.search_pexels + download_image, so API keys and download
    guards (content-type, size, extension resolution) are centralized.
    """
    try:
        results = search_pexels(query, per_page=per_page) or []
    except Exception as e:
        print(f"    ⚠️ Pexels search error for '{query}': {e}")
        return None

    pick = None
    for r in results:
        w = r.get("width") or 0
        if w >= min_width:
            pick = r
            break
    if pick is None and results:
        pick = results[0]

    if pick is None:
        print(f"    ⚠️ No Pexels result for '{query}'")
        return None

    if "src" in pick:
        url = (pick["src"].get("large2x") or pick["src"].get("large")
               or pick["src"].get("medium") or pick["src"].get("original"))
        source = "pexels"
    elif "webformatURL" in pick:
        url = pick.get("webformatURL") or pick.get("largeImageURL")
        source = "pixabay"
    else:
        url = None
        source = "unknown"

    if not url:
        print(f"    ⚠️ No image URL in result for '{query}'")
        return None

    # Resolve final extension before download so idempotency/file-checks align.
    final_out = out_path
    pure = Path(final_out)
    if pure.suffix.lower() not in ALLOWED_OUTPUT_TYPES:
        pure = pure.with_suffix(".jpg")

    if _pexels_download(url, pure):
        # image_search.download_image overrides the suffix with the URL-derived
        # extension (e.g. .jpeg for a JPEG source), so the actual file may not be
        # `pure`. Resolve the real file by same-stem glob to avoid returning a
        # path that doesn't exist.
        actual = _resolve_downloaded(pure)
        return actual if actual else pure
    return None


def _resolve_downloaded(hint: Path) -> Optional[Path]:
    """Find the real file written for `hint` (any allowed extension)."""
    if hint.parent.exists():
        base = hint.stem
        for ext in sorted(ALLOWED_OUTPUT_TYPES, key=len, reverse=True):
            cand = hint.parent / (base + ext)
            if cand.exists():
                return cand
    return None


# ════════════════════════════════════════════════════════════════
#  CLI (optional) — drive the Pexels-first resolver from a shell/agent
# ════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(
        description="Pexels-first body image: search Pexels, download, normalize.")
    ap.add_argument("out_path", help="destination file (e.g. images/image-002.jpg)")
    ap.add_argument("--description", "-d", default="", help="alt/description for query")
    ap.add_argument("--query", "-q", default="", help="explicit Pexels query")
    ap.add_argument("--no-pexels", action="store_true", help="disable Pexels-first")
    ap.add_argument("--long-edge", type=int, default=DEFAULT_LONG_EDGE,
                    help="post-process target long edge (px); 0 disables")
    ap.add_argument("--output-dir", default="", help="confine output to this dir")
    args = ap.parse_args()

    res = resolve_image(
        args.out_path,
        description=args.description,
        query=args.query or None,
        output_dir=args.output_dir or None,
        prefer_pexels=not args.no_pexels,
        allow_generative=False,  # CLI never synthesizes; Pexels-first only
        long_edge=args.long_edge or None,
    )
    print(f"[pexels_images] ok={res.ok} source={res.source} path={res.path}"
          f" reason={res.reason!r}")
    print(f"[pexels_images] query={res.query!r}")
    if res.ok and res.path:
        print(f"Result: {res.path}")
        raise SystemExit(0)
    raise SystemExit(1)
