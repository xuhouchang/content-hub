#!/usr/bin/env python3
"""
Research Card Generator — 替代 enterprise-ai-book-research skill。

从 reports/ 读取素材 → LLM 提炼 → 保存到 research/enterprise-ai-book/cards/
  → 推送到飞书文档（默认启用，可选 --no-fei-shu 跳过）

飞书文档 ID: N64Yd1APeoLbbix5FSmcQUq0nPh

Usage:
  python3 research_cards.py [--date YYYY-MM-DD] [--dry-run] [--max-cards 5] [--no-fei-shu]
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from lib.llm import call_model
from lib.materials import get_all_collected_urls, read_material_content
from lib import quick_relevance_check, slugify

# Try to import shared prompts; fallback to hardcoded string
try:
    from library_system_prompts import RESEARCH_CARD_SYSTEM_PROMPT
except ImportError:
    RESEARCH_CARD_SYSTEM_PROMPT = (
        """你是一个企业AI落地研究助理。"""  # truncated for brevity
    )

# ── paths ──
COLLECTOR_DIR = Path(__file__).parent  # .../workspace
WORKSPACE_DIR = COLLECTOR_DIR           # workspace dir, not parent
RESEARCH_DIR = WORKSPACE_DIR / "research" / "enterprise-ai-book"
CARDS_DIR = RESEARCH_DIR / "cards"
DAILY_DIR = RESEARCH_DIR / "daily"
INDEX_FILE = RESEARCH_DIR / "_index" / "README.md"
ALL_URLS_FILE = RESEARCH_DIR / "_index" / "all_urls.txt"

# ── Feishu Doc Config ──
FEISHU_DOC_TOKEN = "N64Yd1APeoLbbix5FSmcQUq0nPh"
LARK_CLI = "lark-cli"
FEISHU_API_VER = "v1"

# ── Block IDs for each module's "研究素材归档" section ──
# (fetched from outline on first run, hardcoded for speed)
MODULE_BLOCK_IDS = {
    1: "doxcnvof3YDHRwc1wkkRB4VOrsf",  # 一、企业AI认知误区 → 研究素材归档
    2: "doxcneRzvfnM1gkPswIIXjqt3Qf",  # 二、AI与业务流程重构 → 研究素材归档
    3: "doxcnrCmtl8YOn6PG9rQSJ7H3Lg",  # 三、组织人才与绩效 → 研究素材归档
    4: "doxcn5DVAU0vF2BKomhiBc7eOge",  # 四、企业AI系统建设 → 研究素材归档
    5: "doxcnsumeQrQAPEr9rhD10sqhkh",  # 五、AI驱动的企业决策 → 研究素材归档
}

MULTI_MODULE_BLOCK = "doxcnztxbvDaNDLXlL0tQ1Ae0sg"  # 附录：跨模块素材


# ════════════════════════════════════════════════════════════════
#  Card Generation (local)
# ════════════════════════════════════════════════════════════════

def get_existing_card_urls() -> set:
    """Get all URLs already processed into cards."""
    urls = set()
    if ALL_URLS_FILE.exists():
        with open(ALL_URLS_FILE) as f:
            for line in f:
                urls.add(line.strip())
    return urls


def is_url_processed(url: str, existing: set) -> bool:
    """Check if a URL has already been processed (normalized)."""
    return url.strip().rstrip("/") in existing


def filter_new_materials(materials: list[dict], existing_urls: set) -> list[dict]:
    """Filter out already-processed materials. Also apply quick relevance check.

    Uses material content (if available) for relevance checking to avoid
    false negatives from URL-only keyword checks.
    """
    new = []
    for m in materials:
        url = m.get("url", "").strip()
        if not url:
            continue
        if is_url_processed(url, existing_urls):
            continue
        # Try content-based relevance check; fall back to URL-only
        content = read_material_content(url)
        if content:
            result = quick_relevance_check(content[:2000], content[:4000])
        else:
            result = quick_relevance_check(url, url)
        if result == "skip":
            continue
        new.append(m)
    return new


def generate_cards(
    date_str: str,
    max_cards: int = 5,
    max_materials: int = 10,
    model: str = None,
    dry_run: bool = False,
) -> list[dict]:
    """Main card generation flow."""
    print(f"\n{'=' * 60}")
    print(f"📇 Research Cards — {date_str}")
    print(f"{'=' * 60}")

    print("\n1️⃣  Reading collected materials...")
    all_materials = get_all_collected_urls()
    collection_urls = [m for m in all_materials]
    print(f"  Found {len(collection_urls)} materials total")

    existing_urls = get_existing_card_urls()
    new_materials = filter_new_materials(collection_urls, existing_urls)
    print(f"  {len(new_materials)} new (not yet carded)")

    if not new_materials:
        print("  No new materials to process.")
        return []

    print(f"\n2️⃣  Reading material content...")
    scored = []
    seen_urls = set()
    for m in new_materials:
        url = m.get("url", "")
        if not url:
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
        content = read_material_content(url)
        if content:
            scored.append({"url": url, "content": content[:8000], "line": m.get("line", "")})
            print(f"  ✓ {url[:80]}... ({len(content)} chars)")
        else:
            scored.append({"url": url, "content": "", "line": m.get("line", "")})
            print(f"  ⚠️ No saved content for: {url[:80]}")

    print(f"\n3️⃣  Generating cards (max {max_cards})...")
    cards = []
    for i, item in enumerate(scored):
        if len(cards) >= max_cards:
            break
        if not item["content"]:
            continue

        print(f"  Card {i + 1}/{len(scored)}: {item['url'][:60]}...")

        if dry_run:
            print(f"    (dry-run, skip LLM call)")
            continue

        messages = [
            {"role": "system", "content": RESEARCH_CARD_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"""请根据以下素材内容，输出一张研究卡片。

URL: {item['url']}

素材内容：
{item['content']}

请严格按照研究卡片格式输出。""",
            },
        ]

        response = call_model(messages, temperature=0.5, max_tokens=2048, model=model)
        if not response:
            print(f"    ❌ No response from model")
            continue

        card_text = response.strip()
        slug = slugify(item["url"][:80])  # use longer prefix to avoid collisions

        module_match = re.search(r"\*\*对应模块\*\*：(\d)", card_text)
        module = module_match.group(1) if module_match else "0"

        card_path = CARDS_DIR / f"{date_str}_{slug}.md"

        if not dry_run:
            card_path.parent.mkdir(parents=True, exist_ok=True)
            card_text_with_meta = (
                f"""# 研究卡片

- **源URL**: {item['url']}
- **生成日期**: {date_str}

{card_text}

---

*由 research_cards.py 于 {date_str} 自动生成*
"""
            )
            card_path.write_text(card_text_with_meta, encoding="utf-8")

            ALL_URLS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(ALL_URLS_FILE, "a") as f:
                f.write(f"{item['url']}\n")

        cards.append({
            "url": item["url"],
            "module": module,
            "path": str(card_path),
            "slug": slug,
            "card_text": card_text,
        })
        print(f"    ✅ Card saved: {card_path.name}")

    if cards and not dry_run:
        print(f"\n4️⃣  Updating daily summary...")
        DAILY_DIR.mkdir(parents=True, exist_ok=True)
        daily_path = DAILY_DIR / f"{date_str}.md"
        entries = []
        for c in cards:
            entries.append(
                f"- [卡片](./cards/{date_str}_{c['slug']}.md) | 模块 {c['module']} | {c['url']}"
            )
        daily_content = f"# 研究卡片 — {date_str}\n\n" + "\n".join(entries) + "\n"
        daily_path.write_text(daily_content)

        INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(INDEX_FILE, "a") as f:
            f.write(f"\n## {date_str}\n")
            for c in cards:
                f.write(f"- 模块{c['module']} | {c['url'][:60]}... | [卡片](cards/{date_str}_{c['slug']}.md)\n")

    print(f"\n  ✅ Generated {len(cards)} cards")
    return cards


# ════════════════════════════════════════════════════════════════
#  Feishu Push
# ════════════════════════════════════════════════════════════════

def _find_last_block_in_range(start_id: str, end_id: str, fallback: str) -> str:
    """
    Fetch the Feishu doc block range [start_id, end_id] and return the last block ID
    (excluding start_id and end_id themselves). Falls back to fallback on failure.
    """
    try:
        result = subprocess.run(
            [LARK_CLI, "docs", "+fetch", "--api-version", FEISHU_API_VER,
             "--as", "bot", "--doc", FEISHU_DOC_TOKEN,
             "--scope", "range",
             "--start-block-id", start_id,
             "--end-block-id", end_id,
             "--format", "json"],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
        content = data["data"]["document"]["content"]
        # Extract all block IDs from the HTML fragment
        ids = re.findall(r'id="([^"]{20,})"', content)
        # Remove the start/end markers
        filtered = [b for b in ids if b not in (start_id, end_id)]
        if filtered:
            return filtered[-1]
    except Exception as e:
        print(f"    ⚠️ Could not find last block: {e}")
    return fallback


def _shorten_card_for_markdown(card_text: str, date_str: str) -> str:
    """
    Shorten a research card to a compact Markdown snippet for Feishu insertion.
    Lines starting with - (bullets) under ### are included; extensive
    own-viewpoint text is summarized.
    """
    import re as _re

    lines = card_text.split("\n")
    out = []
    in_skip_section = False
    for line in lines:
        stripped = line.strip()
        # Skip long self-opinion blocks
        if stripped.startswith("💡 自有观点") or stripped.startswith("*自有观点*") or stripped.startswith("自有观点："):
            in_skip_section = True
            continue
        if stripped.startswith("📎 来源") or stripped.startswith("**来源**") or stripped.startswith("可引用观点") or stripped.startswith("可信度评级"):
            in_skip_section = False
            continue
        if in_skip_section:
            continue

        # Skip source URLs in markdown format
        if "| https://" in line or "| http://" in line:
            continue

        out.append(line)

    md = "\n".join(out).strip()

    # Truncate to 1500 chars to avoid hitting lark-cli content limits
    if len(md) > 1500:
        md = md[:1497] + "..."

    # Add date footer
    md += f"\n\n*卡片于 {date_str} 自动归档*\n"

    return md


def push_cards_to_feishu(cards: list[dict], date_str: str, dry_run: bool = False) -> bool:
    """
    Push generated cards to the enterprise-ai-book Feishu doc.

    For each card, insert its content into the correct module's "研究素材归档" section.
    """
    print(f"\n5️⃣  Syncing to Feishu doc...")

    if dry_run:
        print("  (dry-run, skip Feishu push)")
        return True

    # Verify lark-cli available
    try:
        subprocess.run([LARK_CLI, "docs", "--api-version", FEISHU_API_VER, "--help"],
                       capture_output=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        print("  ⚠️ lark-cli not available, skipping Feishu push")
        return False

    success_count = 0

    # Map module → list of cards, so we batch per module
    cards_by_mod: dict[int, list[str]] = {}
    for card in cards:
        module = int(card.get("module", 0))
        card_text = card.get("card_text", "")
        if not card_text:
            continue
        cards_by_mod.setdefault(module, []).append(card_text)

    if not cards_by_mod:
        print("  (no cards to push)")
        return False

    # Pre-scan: find the last block in each module's range
    # This avoids hardcoding block IDs that shift as new cards are inserted
    MODULE_RANGES = {
        1: ("doxcnvof3YDHRwc1wkkRB4VOrsf", "doxcnNtOmCKNLFZpqYTrjThcJIh"),
        2: ("doxcneRzvfnM1gkPswIIXjqt3Qf", "doxcnQhfn4trqcr6ztLcGjC9qid"),
        3: ("doxcnrCmtl8YOn6PG9rQSJ7H3Lg", "doxcn6Wkm1yNLsLzqgpN0CjotPb"),
        4: ("doxcn5DVAU0vF2BKomhiBc7eOge", "doxcnWDigMckAY6QfmpJ3CVsqFe"),
        5: ("doxcnsumeQrQAPEr9rhD10sqhkh", "doxcnztxbvDaNDLXlL0tQ1Ae0sg"),
    }
    _MOD_H3 = { 1: MODULE_BLOCK_IDS[1], 2: MODULE_BLOCK_IDS[2],
                3: MODULE_BLOCK_IDS[3], 4: MODULE_BLOCK_IDS[4], 5: MODULE_BLOCK_IDS[5] }

    for module in sorted(cards_by_mod.keys()):
        card_texts = cards_by_mod[module]
        print(f"\n  Module {module}: {len(card_texts)} card(s)")

        # Build combined markdown for this module's cards
        # Each card gets its own section with a heading
        md_parts = []
        for ct in card_texts:
            md = _shorten_card_for_markdown(ct, date_str)
            if md:
                md_parts.append(md)
        if not md_parts:
            print(f"    ⚠️ All cards empty, skipping")
            continue

        combined_md = "\n\n---\n\n".join(md_parts)

        # Determine insert_after: find the last block in this module's range
        mod_anchor_h3 = _MOD_H3.get(module) or MULTI_MODULE_BLOCK
        if module in MODULE_RANGES:
            range_start, range_end = MODULE_RANGES[module]
            insert_after = _find_last_block_in_range(range_start, range_end, mod_anchor_h3)
        else:
            insert_after = mod_anchor_h3  # appendix → fallback to anchor

        print(f"    insert_after: {insert_after}")

        cmd = [
            LARK_CLI, "docs", "+update",
            "--api-version", FEISHU_API_VER,
            "--as", "bot",
            "--doc", FEISHU_DOC_TOKEN,
            "--command", "block_insert_after",
            "--block-id", insert_after,
            "--content", combined_md,
        ]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                success_count += len(card_texts)
                print(f"    ✅ {len(card_texts)} card(s) pushed → module {module}")
            else:
                print(f"    ⚠️ Push failed (module {module}): {result.stderr[:200]}")
                print(f"    stderr: {result.stderr}")
                print(f"    stdout: {result.stdout}")
        except Exception as e:
            print(f"    ⚠️ Push error: {e}")

        time.sleep(2)  # rate limit between modules

    # Also update the sync_status file
    sync_status = f"""# Daily Research Sync — {date_str}

## Status: {'✅ Complete' if success_count > 0 else '⚠️ Partial'}

### Cards Pushed to Feishu ({success_count}/{len(cards)})
"""
    for c in cards:
        sync_status += f"- Module {c.get('module', '?')}: {str(card.get('card_text', ''))[:60]}\n"
    sync_status += (
        f"\n### Feishu Doc\n"
        f"- Doc: {FEISHU_DOC_TOKEN}\n"
    )
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    sync_path = DAILY_DIR / f"{date_str}_sync_status.md"
    sync_path.write_text(sync_status)

    print(f"\n  ✅ Pushed {success_count}/{len(cards)} cards to Feishu")
    return success_count > 0


# ════════════════════════════════════════════════════════════════
#  Main
# ════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Generate research cards and push to Feishu"
    )
    parser.add_argument("--date", type=str, default=None, help="YYYY-MM-DD (default: today)")
    parser.add_argument("--dry-run", action="store_true", help="Skip writing")
    parser.add_argument("--max-cards", type=int, default=5, help="Max cards to generate")
    parser.add_argument("--no-fei-shu", action="store_true", help="Skip Feishu push")
    parser.add_argument(
        "--model",
        type=str,
        default="deepseek-v4-flash",
        help="Model to use (default: deepseek-v4-flash for cards, gpt-5.4 for writing)",
    )
    args = parser.parse_args()

    date_str = args.date or datetime.date.today().isoformat()
    cards = generate_cards(
        date_str=date_str,
        max_cards=args.max_cards,
        model=args.model,
        dry_run=args.dry_run,
    )

    if cards and not args.dry_run and not args.no_fei_shu:
        push_cards_to_feishu(cards, date_str, dry_run=False)
    elif cards:
        if args.no_fei_shu:
            print("  (--no-fei-shu, skip Feishu push)")
        print(f"\n{'=' * 60}")
        print(f"✅ {len(cards)} cards generated (local only)")
        print(f"{'=' * 60}")
        return 0
    else:
        print(f"\n{'=' * 60}")
        print("ℹ️  No cards generated")
        print(f"{'=' * 60}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
