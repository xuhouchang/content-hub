#!/usr/bin/env python3
"""
Filter agent launcher — RSS items.
Reads raw RSS JSON → spawns an agent to do relevance filtering → saves filtered JSON.
Uses `openclaw sessions spawn` to run the filter as an isolated sub-agent.
"""

import sys
import json
import os
import subprocess
import datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import TMP_DIR

# Path to SKILL rules for filter prompt
RULES_PATH = os.path.expanduser("~/.openclaw/skills/research-report-collector/rules.md")


def load_rules():
    if os.path.exists(RULES_PATH):
        with open(RULES_PATH) as f:
            return f.read()
    return ""


def build_filter_prompt(items: list) -> str:
    """Build the prompt for the agent filter."""
    rules = load_rules()
    
    items_json = json.dumps(items, indent=2, ensure_ascii=False)
    
    prompt = f"""You are a content relevance filter for an enterprise AI research system.

Your task: Review each item below and decide if it's relevant to the 9 core topics.

## Core Topic Relevance Rules

{rules}

## Items to Filter

{items_json}

## Output Format

Return ONLY a JSON array of objects. Do NOT include any other text, explanation, or markdown.

For each item in the input array, output either:
- If RELEVANT: {{"index": <0-based index>, "verdict": "pass", "topics": [<list of topic numbers that match>], "reason": "<brief reason>"}}
- If NOT RELEVANT: {{"index": <0-based index>, "verdict": "skip", "reason": "<brief reason>"}}

Keep reasons short (1 sentence max). For pass items, include which of the 9 topics it matches.

Example output:
[
  {{"index": 0, "verdict": "skip", "reason": "Pure model release announcement, no enterprise adoption perspective"}},
  {{"index": 1, "verdict": "pass", "topics": [1, 2], "reason": "Discusses how enterprises are restructuring workflows around AI agents"}}
]
"""
    return prompt


def main():
    date_str = datetime.date.today().isoformat()
    
    # Find latest raw file
    raw_dir = TMP_DIR / "rss"
    if not raw_dir.exists():
        print("No rss raw directory found")
        sys.exit(0)
    
    raw_files = sorted(raw_dir.glob(f"rss_raw_{date_str}.json"))
    if not raw_files:
        print(f"No raw RSS file found for {date_str}")
        sys.exit(0)
    
    raw_path = raw_files[-1]
    with open(raw_path) as f:
        items = json.load(f)
    
    if not items:
        print("No items to filter")
        sys.exit(0)
    
    print(f"Filtering {len(items)} RSS items via agent...")
    
    # Build prompt and save it as a temp file
    prompt = build_filter_prompt(items)
    
    prompt_path = TMP_DIR / "rss" / f"filter_prompt_{date_str}.txt"
    with open(prompt_path, "w") as f:
        f.write(prompt)
    
    # Also save a version with $items for the cron agent
    filter_cmd_path = TMP_DIR / "rss" / f"filter_cmd_{date_str}.json"
    filter_spec = {
        "raw_path": str(raw_path),
        "items_count": len(items),
        "prompt": prompt,
    }
    with open(filter_cmd_path, "w") as f:
        json.dump(filter_spec, f, indent=2)
    
    print(f"Filter prompt saved: {prompt_path}")
    print(f"Filter spec saved: {filter_cmd_path}")
    print(f"Ready for agent processing: {len(items)} items need review")
    
    # Output summary for chaining
    print(f"\nSUMMARY:{json.dumps({'date': date_str, 'source': 'rss_filter', 'items': len(items), 'prompt_file': str(prompt_path)})}")


if __name__ == "__main__":
    main()
