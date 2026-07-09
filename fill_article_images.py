#!/usr/bin/env python3
"""
Fill missing article images by searching Pexels for each placeholder.

Usage:
  python3 fill_article_images.py article.md images/

Looks for ![alt](./images/FILENAME) placeholders in article.md.
For each missing FILENAME, generates search keywords from the alt text,
downloads from Pexels, and saves as FILENAME.
"""

import sys, os, re, json
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from image_search import search_pexels, download_image as pexels_download

# Pexels API key from env
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "IupPtrTI2BG3nKYHvuSBDpZmL4bX48FvCY3HxV4BiN1BvgtQhRJM1to3")

def main():
    article_path = Path(sys.argv[1])
    images_dir = Path(sys.argv[2])
    images_dir.mkdir(parents=True, exist_ok=True)

    text = article_path.read_text(encoding='utf-8')

    # Find all image placeholders: ![alt](./images/FILENAME)
    placeholders = re.findall(r'!\[(.*?)\]\(\./images/([^)]+)\)', text)
    # Also look for ![alt](images/FILENAME)
    placeholders += re.findall(r'!\[(.*?)\]\(images/([^)]+)\)', text)

    if not placeholders:
        print("No image placeholders found.")
        return 1

    print(f"Found {len(placeholders)} image placeholders.")

    for alt_text, filename in placeholders:
        output_path = images_dir / filename
        if output_path.exists() and output_path.stat().st_size > 5000:
            print(f"  ✅ {filename} — already exists, skipping")
            continue

        print(f"\n  🔍 Searching for: '{filename}' (alt: '{alt_text}')")

        # Generate search keywords from alt text and filename
        keywords = generate_keywords(alt_text, filename)

        found = False
        for kw in keywords:
            print(f"    Pexels query: '{kw}'")
            try:
                results = search_pexels(kw, per_page=5)
                if results:
                    for photo in results:
                        url = photo.get("src", {}).get("large2x") or photo.get("src", {}).get("original")
                        if url:
                            print(f"    Downloading from {url[:60]}...")
                            if pexels_download(url, str(output_path)):
                                size = output_path.stat().st_size
                                print(f"    ✅ Downloaded {filename} ({size/1024:.0f} KB)")
                                found = True
                                break
                if found:
                    break
            except Exception as e:
                print(f"    ⚠️ Error: {e}")
                continue

        if not found:
            print(f"    ❌ Could not find image for {filename}")

    print(f"\nDone. Images in {images_dir}:")
    for f in sorted(images_dir.iterdir()):
        print(f"  {f.name} ({f.stat().st_size/1024:.0f} KB)")


def generate_keywords(alt_text: str, filename: str) -> list:
    """Generate Pexels search keywords from alt text and filename."""
    # Remove extension and number prefix
    stem = Path(filename).stem
    stem_clean = re.sub(r'^\d+-', '', stem)
    stem_clean = re.sub(r'image-\d+-', '', stem_clean)
    stem_clean = stem_clean.replace('-', ' ').replace('_', ' ')

    # Use the alt text as primary keyword
    keywords = []

    # Chinese alt text → translate to English keywords
    translation_map = {
        "安全架构图": "cybersecurity network architecture",
        "数据访问对比": "data access security comparison",
        "攻击面分析": "attack surface analysis cybersecurity",
        "营销人员AI恐惧数据": "marketer AI fear data chart",
        "高管AI投资驱动因素": "executive AI investment driving",
        "员工AI使用能力对比": "employee AI capability comparison",
        "OpenClaw架构示意图": "AI agent open source architecture",
        "云端vs本地Agent对比": "cloud vs local AI agent comparison",
        "微软Build大会截图": "Microsoft Build conference keynote",
        "Agent变种对比": "AI agent variants comparison technology",
        "Nemotron Prompt Atlas可视化界面": "NVIDIA Nemotron visualization AI data",
        "合成数据与真实数据对比": "synthetic data vs real data comparison AI",
    }

    if alt_text in translation_map:
        keywords.append(translation_map[alt_text])
    elif stem_clean:
        keywords.append(stem_clean)

    # Fallback keywords
    fallbacks = {
        "security": ["cybersecurity technology abstract", "network security concept"],
        "ai": ["artificial intelligence concept", "AI technology abstract"],
        "data": ["data visualization technology", "big data analytics"],
        "marketing": ["business marketing strategy", "corporate meeting discussion"],
        "architecture": ["technology infrastructure", "system architecture diagram"],
        "agent": ["AI robot assistant", "automation technology concept"],
        "comparison": ["business comparison analysis", "technology options concept"],
        "cloud": ["cloud computing concept", "server technology abstract"],
    }

    for key, fallback_list in fallbacks.items():
        if key in stem_clean.lower() or key in alt_text.lower():
            keywords.extend(fallback_list)
            break

    if not keywords:
        keywords.append("technology concept abstract professional")

    return keywords[:3]


if __name__ == "__main__":
    sys.exit(main())
