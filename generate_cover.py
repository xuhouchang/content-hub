#!/usr/bin/env python3
"""
Generate article cover image from Pexels search + Pillow crop/resize.

Flow:
  1. Accept article title + content (story)
  2. LLM generates 2-3 English Pexels search keywords based on content
  3. Search Pexels → download top result (landscape orientation)
  4. Crop to 2.35:1 (WeChat article top cover) and 1:1 (list thumbnail)
  5. Save to article's images/ directory

Usage:
  python3 generate_cover.py <article-title> <images-dir> [--content <article-content>] [--dry-run]

Output:
  - {images_dir}/cover-wide.jpg   (2.35:1, for article top)
  - {images_dir}/cover-1x1.jpg    (1:1, for list thumbnail)
"""

import argparse
import io
import os
import re
import sys
from pathlib import Path

from PIL import Image

# Use image_search module for Pexels search
sys.path.insert(0, str(Path(__file__).parent))
from lib.llm import call_model, DEFAULT_WRITING_MODEL
from image_search import search_pexels, download_image as pexels_download

# ── Cover aspect ratios (WeChat requirements) ──
COVER_WIDE_RATIO = 2.35 / 1   # 2.35:1, recommended width >= 1080px
COVER_1X1_RATIO = 1.0 / 1.0   # 1:1, for list thumbnail
TARGET_WIDTH = 1200            # px, quality standard
TARGET_HEIGHT_WIDE = int(TARGET_WIDTH / COVER_WIDE_RATIO)  # ~510px
TARGET_HEIGHT_1X1 = TARGET_WIDTH


def generate_search_keywords(title: str, content: str = "") -> list[str]:
    """
    Use LLM to generate 2-3 English Pexels search keywords based on the article.
    Returns most relevant keyword first.
    """
    story = content[:2000] if content else title
    prompt = f"""You are helping generate Pexels stock photo search keywords for a WeChat article cover image.

Article title: {title}

Key content to illustrate:
{story[:1500]}

Your job: generate 2-3 English search keywords (comma-separated, single line) that would find a high-quality, contextually relevant stock photo on Pexels.com to serve as the article's cover image.

Rules:
- Keywords should be concrete nouns/adjectives, not abstract concepts
- If the article mentions a specific company (OpenAI, Anthropic, Salesforce, Google) or person, include it
- If the article is about a trend/industry, use broad professional imagery
- Prefer "business", "technology", "office", "professional" themed photos over abstract
- One short keyword per concept, maximum 3 keywords
- Output ONLY the keywords, one per line, no numbering
- Example: "OpenAI office\nAI robot hand\nfuturistic data center"
- Example: "enterprise boardroom\npensive business man\nteam meeting"
- Example: "network server rack\nblue digital lines\nvirtual dashboard"

Keywords:"""

    messages = [
        {"role": "system", "content": "You are a stock photo search query generator for professional business articles."},
        {"role": "user", "content": prompt},
    ]

    response = call_model(messages, temperature=0.4, max_tokens=200, model="deepseek-v4-flash")
    if not response:
        return ["business technology", "enterprise digital", "professional workspace"]

    keywords = [k.strip() for k in response.strip().split("\n") if k.strip()]
    # Filter to top 3
    return keywords[:3]


def crop_center(image: Image.Image, target_ratio: float) -> Image.Image:
    """Crop image to target aspect ratio from center."""
    w, h = image.size
    current_ratio = w / h

    if abs(current_ratio - target_ratio) < 0.01:
        return image  # Already matching

    if current_ratio > target_ratio:
        # Image is wider than target - crop horizontal center
        new_w = int(h * target_ratio)
        left = (w - new_w) // 2
        return image.crop((left, 0, left + new_w, h))
    else:
        # Image is taller than target - crop vertical center
        new_h = int(w / target_ratio)
        top = (h - new_h) // 2
        return image.crop((0, top, w, top + new_h))


def generate_cover(
    title: str,
    images_dir: str,
    content: str = "",
    dry_run: bool = False,
) -> tuple[bool, bool]:
    """
    Generate cover images by:
    1. LLM → search keywords
    2. Pexels search → download
    3. Pillow crop to 2.35:1 + 1:1

    Returns (wide_ok, square_ok).
    """
    img_dir = Path(images_dir)
    img_dir.mkdir(parents=True, exist_ok=True)

    wide_path = img_dir / "cover-wide.jpg"
    square_path = img_dir / "cover-1x1.jpg"

    # Step 1: Generate search keywords
    print(f"  Generating search keywords for: {title[:40]}...")
    keywords = generate_search_keywords(title, content)
    print(f"  Keywords: {', '.join(keywords[:3])}")

    if dry_run:
        print(f"  (dry-run, skip download/crop)")
        return True, True

    # Step 2: Search Pexels
    downloaded = False
    for kw in keywords:
        print(f"  Searching Pexels: '{kw}'...")
        results = search_pexels(kw, per_page=5)
        if results:
            # Pick the first good result (landscape preferred, large enough)
            pick = None
            for r in results:
                w = r.get("width", 0)
                if w >= 800:
                    pick = r
                    break
            if not pick and results:
                pick = results[0]

            if pick:
                url = pick.get("src", {}).get("large2x") or pick.get("src", {}).get("large")
                photographer = pick.get("photographer", "Unknown")
                if url:
                    # Determine extension from URL
                    ext = ".jpg"
                    if ".jpeg" in url.lower():
                        ext = ".jpeg"
                    elif ".png" in url.lower():
                        ext = ".png"
                    elif ".webp" in url.lower():
                        ext = ".webp"
                    temp_path = img_dir / f"_cover_temp{ext}"
                    print(f"    Downloading (photo by {photographer})...")
                    if pexels_download(url, temp_path):
                        downloaded = True
                        break
                    else:
                        print(f"    ⚠️ Download failed for {kw}, trying next...")
        if not downloaded:
            print(f"    No suitable image found for '{kw}', trying next keyword...")

    if not downloaded:
        print(f"  ⚠️ No suitable image found on Pexels")
        return False, False

    # Step 3: Crop and save
    try:
        img = Image.open(temp_path)

        # Convert to RGB if RGBA/PNG
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        # Crop and resize for wide cover (2.35:1)
        wide_img = crop_center(img, COVER_WIDE_RATIO)
        wide_img = wide_img.resize((TARGET_WIDTH, TARGET_HEIGHT_WIDE), Image.LANCZOS)
        wide_img.save(wide_path, "JPEG", quality=90)
        print(f"    ✅ Wide cover: {wide_path.name} ({wide_img.size[0]}x{wide_img.size[1]})")

        # Crop and resize for square cover (1:1)
        square_img = crop_center(img, COVER_1X1_RATIO)
        square_img = square_img.resize((TARGET_WIDTH, TARGET_HEIGHT_1X1), Image.LANCZOS)
        square_img.save(square_path, "JPEG", quality=90)
        print(f"    ✅ Square cover: {square_path.name} ({square_img.size[0]}x{square_img.size[1]})")

        # Cleanup temp
        if temp_path.exists():
            temp_path.unlink()

        return True, True

    except Exception as e:
        print(f"    ❌ Crop error: {e}")
        return False, False


def generate_cover_from_file(
    article_path: str,
    images_dir: str,
    dry_run: bool = False,
) -> tuple[bool, bool]:
    """Read article.md, extract title and content, then generate cover."""
    with open(article_path, encoding="utf-8") as f:
        content = f.read()

    title_match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else "Untitled"

    return generate_cover(
        title=title,
        images_dir=images_dir,
        content=content[:3000],
        dry_run=dry_run,
    )


def main():
    parser = argparse.ArgumentParser(description="Generate article cover image from Pexels")
    parser.add_argument("--title", type=str, default=None, help="Article title")
    parser.add_argument("--article", type=str, default=None, help="Path to article.md (alternative to --title)")
    parser.add_argument("--images-dir", "-i", required=True, help="Images output directory")
    parser.add_argument("--dry-run", action="store_true", help="Skip download/crop")
    args = parser.parse_args()

    if args.article:
        wide_ok, square_ok = generate_cover_from_file(
            article_path=args.article,
            images_dir=args.images_dir,
            dry_run=args.dry_run,
        )
    elif args.title:
        wide_ok, square_ok = generate_cover(
            title=args.title,
            images_dir=args.images_dir,
            dry_run=args.dry_run,
        )
    else:
        print("❌ Provide --title or --article")
        return 1

    if wide_ok and square_ok:
        print(f"✅ Cover images generated")
        return 0
    else:
        print(f"⚠️ Cover generation partial: wide={wide_ok}, square={square_ok}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
