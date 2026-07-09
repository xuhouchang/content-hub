#!/usr/bin/env python3
"""
Material tagging pipeline: use LLM to tag every collected article with
multi-dimensional labels (7 dimensions: form, topic, perspective,
evidence_source, evidence_depth, tone, entity).

Usage:
  python3 tag_materials.py [--date YYYY-MM-DD] [--force] [--dry-run]

The tag data is stored in wechat-articles/_material_tags.json, keyed by URL.
Only new (untagged) materials are processed, unless --force is given.

For new materials, the LLM outputs 7 dimensions (evidence_source + evidence_depth
replace the old single evidence_type). Old entries with evidence_type are
kept and normalized during similarity computation.
"""

import argparse
import datetime
import json
import os
import sys
import time
from pathlib import Path

from lib.llm import call_model
from lib.materials import get_all_collected_urls, read_material_content
from tag_schema import TAG_SCHEMA, TAG_SYSTEM_PROMPT

COLLECTOR_DIR = Path(__file__).parent
WORKSPACE_DIR = COLLECTOR_DIR.parent
OUTPUT_BASE = WORKSPACE_DIR / "wechat-articles"
TAGS_FILE = OUTPUT_BASE / "_material_tags.json"

# ── How many materials to send per LLM batch ──
BATCH_SIZE = 10
# ── Minimum content chars to justify tagging ──
MIN_CONTENT_CHARS = 300
# ── Max batches per run (to stay within cost/token limits) ──
MAX_BATCHES = 5  # = 50 materials max per run

# ── Active dimensions for NEW tagging (excludes deprecated evidence_type) ──
TAG_DIMENSIONS = [
    "content_form", "topic_focus", "perspective",
    "evidence_source", "evidence_depth", "tone", "entity_scope",
]

# ── Build a compact dimension summary for the LLM prompt ──
# Only include active dimensions, skip deprecated evidence_type
def _build_tag_definitions() -> str:
    lines = []
    for dim in TAG_DIMENSIONS:
        info = TAG_SCHEMA.get(dim, {})
        desc = info.get("description", dim).replace("[已弃用] ", "")
        lines.append(f"## {desc} (key: {dim})")
        for tag in info.get("tags", []):
            lines.append(f"- {tag}")
        lines.append("")
    return "\n".join(lines)


# ── Load existing tag database ──
def load_existing_tags() -> dict:
    """Load existing tags dict (URL -> tags)."""
    if TAGS_FILE.exists():
        try:
            return json.loads(TAGS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, Exception):
            return {}
    return {}


# ── Save tag database ──
def save_tags(tags: dict):
    TAGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    TAGS_FILE.write_text(json.dumps(tags, ensure_ascii=False, indent=2), encoding="utf-8")


# ── Pick untagged materials ──
def find_untagged(collected: list[dict], existing: dict, force: bool = False) -> list[dict]:
    untagged = []
    for m in collected:
        url = m.get("url", "")
        if not url:
            continue
        if not force and url in existing:
            continue  # already tagged
        content = read_material_content(url)
        if content and len(content) >= MIN_CONTENT_CHARS:
            m["content"] = content
            untagged.append(m)
        elif content:
            pass  # too short, skip
        else:
            pass  # no content, skip
    return untagged


# ── Call LLM to tag a batch of materials ──
def tag_batch(batch: list[dict], batch_index: int) -> list[dict]:
    """Send a batch of materials to LLM for tagging. Returns list of tag dicts."""
    tag_defs = _build_tag_definitions()
    dim_keys_str = ", ".join(TAG_DIMENSIONS)

    materials_block = ""
    for i, m in enumerate(batch):
        title = m.get("title", "") or Path(m.get("url", "")).stem
        content = (m.get("content", "") or "")
        content_preview = content[:2000]
        materials_block += f"""
### Material {i+1}
URL: {m['url']}
Title: {title}
Content:
{content_preview}
---end---
"""

    user_prompt = f"""Below are {len(batch)} materials to tag. For each one, read the content and assign tags from all {len(TAG_DIMENSIONS)} dimensions.

Tag schema:
{tag_defs}

Materials to tag:
{materials_block}

Output a JSON array with {len(batch)} entries (one per material, in the same order). Each entry must have:
- "url": the original URL
- "title": the original title
- "tags": an object with the following keys: {dim_keys_str}
- "key_signal": 1-2 sentence summary of the most surprising/noteworthy finding (empty string if none)

IMPORTANT: Only output the JSON array, nothing else."""

    messages = [
        {"role": "system", "content": TAG_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    response = call_model(messages, temperature=0.3, max_tokens=4096, model="deepseek-chat")
    if not response:
        print(f"  ❌ Batch {batch_index}: LLM returned no response")
        return []

    # Try to extract JSON from response
    try:
        start = response.find("[")
        end = response.rfind("]")
        if start >= 0 and end > start:
            json_str = response[start:end+1]
            results = json.loads(json_str)
            if isinstance(results, list):
                return results
        print(f"  ⚠️ Batch {batch_index}: Could not extract JSON from response")
        print(f"  Response preview: {response[:200]}")
        return []
    except json.JSONDecodeError as e:
        print(f"  ⚠️ Batch {batch_index}: JSON parse error: {e}")
        print(f"  Response preview: {response[:300]}")
        return []


# ── Main ──
def main():
    parser = argparse.ArgumentParser(description="Tag collected materials with LLM")
    parser.add_argument("--date", type=str, default=None, help="Date for tagging context")
    parser.add_argument("--force", action="store_true", help="Re-tag already tagged materials")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be tagged without calling LLM")
    parser.add_argument("--max-batches", type=int, default=MAX_BATCHES, help=f"Max batches (default: {MAX_BATCHES})")
    args = parser.parse_args()

    date_str = args.date or datetime.date.today().isoformat()

    print(f"{'='*60}")
    print(f"📋 Material Tagging Pipeline — {date_str}")
    print(f"{'='*60}")

    # 1. Load existing tags
    existing = load_existing_tags()
    print(f"\n1️⃣  Existing tags: {len(existing)} materials")

    # 2. Get collected materials
    print("\n2️⃣  Collecting materials...")
    collected = get_all_collected_urls()
    print(f"  Found {len(collected)} collected items")

    # 3. Find untagged
    print("\n3️⃣  Finding untagged materials...")
    untagged = find_untagged(collected, existing, force=args.force)
    print(f"  {len(untagged)} materials need tagging")

    if not untagged:
        print("  ✅ All materials already tagged!")
        return

    # 4. Batch and call LLM
    print(f"\n4️⃣  Tagging in batches of {BATCH_SIZE}...")

    total_tagged = 0
    tag_results = dict(existing)  # start with existing

    max_batches = min(args.max_batches, (len(untagged) + BATCH_SIZE - 1) // BATCH_SIZE)

    for batch_idx in range(max_batches):
        batch_start = batch_idx * BATCH_SIZE
        batch_end = min(batch_start + BATCH_SIZE, len(untagged))
        batch = untagged[batch_start:batch_end]

        print(f"\n  Batch {batch_idx + 1}/{max_batches} "
              f"({batch_start+1}-{batch_end} of {len(untagged)})")

        if args.dry_run:
            for m in batch:
                print(f"    [dry-run] Would tag: {m['url'][:70]}")
            continue

        results = tag_batch(batch, batch_idx + 1)
        if not results:
            print(f"  ⚠️  Batch {batch_idx + 1} failed, skipping")
            continue

        # Merge results into tag database
        for r in results:
            url = r.get("url", "").rstrip("/")
            if url and "tags" in r:
                tag_results[url] = {
                    "title": r.get("title", ""),
                    "tags": r["tags"],
                    "key_signal": r.get("key_signal", ""),
                    "tagged_at": datetime.datetime.now().isoformat(),
                }
                total_tagged += 1

        # Save after each batch (partial save safety)
        save_tags(tag_results)
        print(f"  ✅ Batch {batch_idx + 1}: tagged {len(results)} materials (total: {total_tagged})")

        # Small delay between batches to avoid rate limits
        if batch_idx < max_batches - 1:
            time.sleep(2)

    print(f"\n{'='*60}")
    print(f"✅ Tagging complete: {total_tagged} new tags, {len(tag_results)} total")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
