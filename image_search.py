#!/usr/bin/env python3
"""
Image search for article illustrations.

Search Pexels (primary) and Pixabay (fallback), download top match.

Usage:
  python3 image_search.py "<query>" <output_dir> [--index N]
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path


# ── Config ──
# Keys are read from the environment (set in .env or the shell). They are NOT
# hardcoded here anymore — the previously committed keys were leaked and have
# been rotated. If a key is missing, that provider is skipped gracefully and we
# fall back to a local placeholder instead of silently producing no image.
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")
PEXELS_URL = "https://api.pexels.com/v1/search"
PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY", "")
PIXABAY_URL = "https://pixabay.com/api/"

# Local placeholder used when no remote image can be fetched (zero external
# dependency). Generated asset under assets/; committed via force-add.
PLACEHOLDER_PATH = Path(__file__).resolve().parent / "assets" / "placeholder.jpg"

# Allowed image types
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

# Maximum image file size (5 MB)
MAX_FILE_SIZE = 5 * 1024 * 1024


def _use_placeholder(output_path: Path, index: int) -> bool:
    """Copy the local placeholder to image-<index>-pexels.jpg.

    Returns True if a placeholder was written (so the caller can treat this as
    a (degraded) success and the article still renders with a real file).
    """
    if not PLACEHOLDER_PATH.exists():
        return False
    target = output_path.parent / f"image-{index:03d}-pexels.jpg"
    try:
        target.write_bytes(PLACEHOLDER_PATH.read_bytes())
        print(f"    ⚠️ No remote image; used local placeholder → {target.name}")
        return True
    except OSError as e:
        print(f"    ⚠️ Placeholder copy failed: {e}")
        return False


def search_pexels(
    query: str,
    per_page: int = 5,
    page: int | None = None,
    max_random_page: int = 6,
    exclude_ids: set | frozenset | None = None,
) -> list[dict]:
    """Search Pexels and return list of photo dicts.

    Args:
        query:        Pexels search string.
        per_page:     page size.
        page:         explicit Pexels page. If None, a random page in
                      [1, max_random_page] is chosen so repeated searches for
                      the same query do NOT always return the same page-1
                      thumbs (the historical cause of reused top-1 photos).
        max_random_page: upper bound for the random page when ``page`` is None.
        exclude_ids:  photo IDs (Pexels numeric slug) to filter out client-side.
                      Best-effort dedup; the authoritative persistent ledger
                      lives in lib.pexels_images.
    """
    if not PEXELS_API_KEY:
        print("    ⚠️ PEXELS_API_KEY not set; skipping Pexels")
        return []
    import random as _random
    eff_page = page if page is not None else (
        _random.randint(1, max(1, max_random_page)))
    params = urllib.parse.urlencode({
        "query": query,
        "per_page": per_page,
        "page": eff_page,
        "orientation": "landscape",
    })
    url = f"{PEXELS_URL}?{params}"
    req = urllib.request.Request(url, headers={
        "Authorization": PEXELS_API_KEY,
        "User-Agent": "Mozilla/5.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            photos = data.get("photos", [])
        if exclude_ids:
            photos = [
                ph for ph in photos if not _photo_id(ph) in exclude_ids
            ]
        return photos
    except Exception as e:
        print(f"    ⚠️ Pexels search failed: {e}")
        return []


def _photo_id(photo: dict) -> str | None:
    """Best-effort photo ID from a Pexels (or Pixabay) photo dict."""
    # Pexels photo dicts carry an integer ``id``.
    pid = photo.get("id")
    if pid is not None:
        return str(pid)
    src_id = photo.get("src", {}).get("id")
    if src_id is not None:
        return str(src_id)
    return None

def search_pixabay(query: str, per_page: int = 5) -> list[dict]:
    """Search Pixabay and return list of image dicts."""
    if not PIXABAY_API_KEY:
        print("    ⚠️ PIXABAY_API_KEY not set; skipping Pixabay")
        return []
    params = urllib.parse.urlencode({
        "key": PIXABAY_API_KEY,
        "q": query,
        "per_page": per_page,
        "image_type": "photo",
        "orientation": "horizontal",
        "safesearch": "true",
    })
    url = f"{PIXABAY_URL}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            return data.get("hits", [])
    except Exception as e:
        print(f"    ⚠️ Pixabay search failed: {e}")
        return []


def download_image(url: str, output_path: Path) -> bool:
    """Download image from URL to output_path. Returns success."""
    ext = Path(urllib.parse.urlparse(url).path).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        ext = ".jpg"

    # Enforce extension
    final_path = output_path.with_suffix(ext)

    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            content_type = resp.headers.get("Content-Type", "")
            data = resp.read()

            # Check file size
            if len(data) > MAX_FILE_SIZE:
                print(f"    ⚠️ Image too large ({len(data) / 1024 / 1024:.1f} MB), skipping")
                return False

            # Check content type
            if "image" not in content_type:
                print(f"    ⚠️ Not an image (Content-Type: {content_type}), skipping")
                return False

            # Infer extension from content type if not already set
            if ext == ".jpg" and "png" in content_type:
                final_path = final_path.with_suffix(".png")

            final_path.write_bytes(data)
            size_kb = len(data) / 1024
            print(f"    ✓ Downloaded: {final_path.name} ({size_kb:.0f} KB)")
            return True

    except Exception as e:
        print(f"    ⚠️ Download failed: {e}")
        return False


def search(query: str) -> list[dict]:
    """Search Pexels first, fall back to Pixabay."""
    results = search_pexels(query)
    if results:
        return results

    # Fallback to Pixabay
    pixabay_results = search_pixabay(query)
    if pixabay_results:
        return pixabay_results

    return []


def main():
    parser = argparse.ArgumentParser(description="Search and download images for articles")
    parser.add_argument("query", type=str, help="Search query")
    parser.add_argument("output_dir", type=str, help="Output directory for downloaded images")
    parser.add_argument("--index", type=int, default=1, help="Image index in the article (1-based)")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not PEXELS_API_KEY and not PIXABAY_API_KEY:
        print("⚠️ PEXELS_API_KEY / PIXABAY_API_KEY 均缺失：远程配图将全部走本地 placeholder 兜底")

    # R1: self-degrade to the local placeholder when the global image-search
    # budget is nearly exhausted, so the writer finishes and publishes instead
    # of being killed at the 900s parent limit. The parent loop also guards
    # this, but we double-check here since this script runs as a subprocess.
    try:
        _img_deadline = float(os.environ.get("IMAGE_SEARCH_DEADLINE", "0"))
    except ValueError:
        _img_deadline = 0.0
    if _img_deadline and time.time() > _img_deadline - 60:
        print(f"  ⏱️ Image-search budget nearly exhausted; "
              f"using placeholder for: {args.query}")
        if _use_placeholder(Path(args.output_dir) / "image-001-pexels", args.index):
            sys.exit(0)
        print(f"  ❌ No placeholder available either; giving up")
        sys.exit(1)

    # Search
    photos = search(args.query)

    if not photos:
        print(f"  ⚠️ No remote images for: {args.query}")
        # Fallback to a local placeholder so the article still has a valid file
        # (no broken/empty image reference) instead of silently failing.
        if _use_placeholder(Path(args.output_dir) / "image-001-pexels", args.index):
            sys.exit(0)
        print(f"  ❌ No placeholder available either; giving up")
        sys.exit(1)

    # Try to download the best one
    for photo in photos:
        # Pexels format
        url = None
        if "src" in photo:
            # Try medium then original
            url = photo["src"].get("medium") or photo["src"].get("original")
            # Prefer landscape for article headers
            if photo["src"].get("landscape"):
                url = photo["src"]["landscape"]
        # Pixabay format
        elif "webformatURL" in photo:
            url = photo.get("webformatURL") or photo.get("largeImageURL")

        if not url:
            continue

        # Remove query params for clean name
        clean_query = args.query.replace(" ", "-").lower()[:30]
        output_path = output_dir / f"image-{args.index:03d}-pexels"
        if download_image(url, output_path):
            # Create metadata json
            meta_path = output_dir / "metadata.json"
            meta = {}
            if meta_path.exists():
                with open(meta_path) as f:
                    meta = json.load(f)
            meta[str(args.index)] = {
                "query": args.query,
                "source": "pexels" if "src" in photo else "pixabay",
                "url": url,
            }
            with open(meta_path, "w") as f:
                json.dump(meta, f, indent=2)
            sys.exit(0)

    print(f"  ❌ Failed to download any image for: {args.query}")
    sys.exit(1)


if __name__ == "__main__":
    main()
