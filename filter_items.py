#!/usr/bin/env python3
"""
Filter collected items using LLM API (OpenAI-compatible) and write filtered JSON.

Reads raw JSON from tmp/<source>/<source>_raw_YYYY-MM-DD.json,
calls LLM API to determine pass/skip per item,
writes verdicts to tmp/<source>/<source>_filtered_YYYY-MM-DD.json.

Uses DeepSeek Chat API (same as default agent model).
All logic in Python, zero agent prompt dependencies.
"""

import sys
import os
import json
import os
import time
import re
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import fetch_url

# ── Config ──
DEFAULT_API_BASE = "https://api.deepseek.com/v1"
DEFAULT_API_KEY = os.environ.get("DEEPSEEK_API_KEY", os.environ.get("LLM_API_KEY", ""))
DEFAULT_MODEL = "deepseek-v4-flash"

COLLECTOR_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
TMP_DIR = COLLECTOR_DIR / "tmp"

# Override from environment (for testing or switching providers)
API_BASE = os.environ.get("FILTER_API_BASE", DEFAULT_API_BASE)
API_KEY = os.environ.get("FILTER_API_KEY", DEFAULT_API_KEY)
MODEL = os.environ.get("FILTER_MODEL", DEFAULT_MODEL)


# ── Filter Rules (embedded, not prompt-based) ──

TOPIC_LABELS = {
    1: "AI Workflows & Productivity — AI对工作的影响、工作流重塑",
    2: "Enterprise & Organizational Adoption — AI在企业组织内的应用、B端落地",
    3: "Capability Models & Reskilling — AI时代的能力模型定义、人才转型",
    4: "Organizational Structure — AI时代的组织结构变革、超级个体/超级小队",
    5: "Learning in the AI Era — AI时代的学习方式重塑、企业内部培训",
    6: "Performance & Compensation — AI时代的绩效评估与激励体系演变",
    7: "Leadership & Change Management — 领导力变迁与组织变革管理",
    8: "Human-AI Collaboration — 人机协同模式、Agentic团队运作、信任机制",
    9: "AI Governance & Ethics — 企业内部影子AI治理、合规与风险管理",
    10: "Surprising / Interesting AI Research Findings — 有意思的AI研究发现（反常识、出人意料的实验结果、模型行为揭示、可解释性发现等，不强制要求关联企业落地场景）",
}

SYSTEM_PROMPT = """You are a content relevance filter for an Enterprise AI research library.

For each item, determine if it passes the Enterprise AI Relevance Filter.

## Core Topics (at least one must match to PASS)
1. AI Workflows & Productivity — AI impact on work, workflow redesign, productivity
2. Enterprise & Organizational Adoption — AI deployment in enterprises, B2B use cases
3. Capability Models & Reskilling — AI-era skill models, talent transformation
4. Organizational Structure — Org structure changes from AI, super-individuals/super-teams
5. Learning in the AI Era — Learning reshaping, corporate training
6. Performance & Compensation — AI-era performance review, incentives, OKR/KPI
7. Leadership & Change Management — Leadership shifts, change management, digital transformation
8. Human-AI Collaboration — Human-machine teaming, agentic workflows, trust mechanisms
9. AI Governance & Ethics — Shadow AI governance, compliance, risk management
10. Surprising / Interesting AI Research Findings — Counterintuitive findings, unexpected experiment results, model behavior insights, interpretability discoveries. NOT required to have enterprise adoption angle.

## PASS Criteria
PASS if the item has ANY of:

**Enterprise AI topics (topics 1-9):**
- Enterprise case studies, industry reports, application practices
- Organizational change data or workforce transformation insights
- Management/executive decision-making perspectives
- Workflow embedding AI, business process changes
- ROI/efficiency data related to AI adoption
- Training, capability models, or reskilling frameworks
- Trust, governance, compliance, or ethics discussions in enterprise context
- Agentic workflows, multi-agent systems, or human-AI collaboration patterns

**Interesting/Surprising Research findings (topic 10):**
- Counterintuitive results from AI research (model does something unexpected)
- AI interpretability findings that reveal how models "think"
- Experiments that surface surprising model behaviors (deception, theory of mind, etc.)
- Data-supported findings on who actually benefits from AI and how
- Unexpected observations about AI usage patterns and their real-world effects
- Cognitive science / psychology of LLMs — what models reveal about intelligence
- Aha-moment findings that would interest a general reader (not just ML engineers)

## SKIP Criteria
SKIP if the item is:
- Pure technical release/announcement (no enterprise or interesting-finding angle)
- Agent framework/tooling review or comparison
- Model benchmark or benchmark-focused technical paper
- Personal consumer product experience/blog ("I tried X tool" — unless it reveals broad surprising usage data)
- Investment/funding news
- Open source project release without context
- Tutorials, how-to guides, prompt engineering tips

## Output Format
Return a JSON array, same order as input. Each element:
{"index": N, "verdict": "pass"|"skip", "topics": [list of topic numbers], "reason": "brief reason"}
"""


def _fetch_page_text(url: str, max_chars: int = 3000) -> str:
    """Fetch a URL and extract readable text content.
    Used when summary/content is absent from the collected item.
    Uses unified fetch_url with Jina fallback.
    """
    try:
        result = fetch_url(url, timeout=20)
        if not result:
            return "[fetch failed: no content]"
        # If it's HTML, strip tags
        if "<" in result[:500] and ">" in result[:500]:
            text = re.sub(r"<style[^>]*>.*?</style>", "", result, flags=re.DOTALL|re.IGNORECASE)
            text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL|re.IGNORECASE)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text).strip()
        else:
            text = result.strip()
        return text[:max_chars]
    except Exception as e:
        return f"[fetch error: {e}]"


def build_filter_prompt(items: list) -> str:
    """Build a prompt with the items to filter.
    When summary/content is absent, fetches page text from URL.
    """
    parts = ["Review each item and determine if it should be PASS or SKIP.\n"]
    for i, item in enumerate(items):
        title = item.get("title", "").strip()
        url = item.get("url", "").strip()
        summary = item.get("summary", "").strip()
        if len(summary) < 100:
            print(f"  Fetched content for [{i}] {title[:40]}")
            summary = _fetch_page_text(url)
        summary = summary[:2000]
        parts.append(
            f"[{i}] Title: {title}\n"
            f"    URL: {url}\n"
            f"    Summary: {summary}\n"
        )
    parts.append("\nOutput the JSON array only, no extra text.")
    return "\n".join(parts)


def call_llm(messages: list, max_retries: int = 3) -> str | None:
    """Call OpenAI-compatible API for chat completion. Retries on failure."""
    import httpx

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "model": MODEL,
        "messages": messages,
        "temperature": 0.1,
        # 不限制 max_tokens（老板要求：DeepSeek 不设 Max token 限制），让生成自然完成
    }

    # Determine API endpoint
    base = API_BASE.rstrip("/")
    if not base.endswith("/v1"):
        url = f"{base}/v1/chat/completions"
    else:
        url = f"{base}/chat/completions"

    for attempt in range(max_retries):
        try:
            with httpx.Client(timeout=120) as client:
                resp = client.post(url, headers=headers, json=body)
            if resp.status_code == 200:
                data = resp.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                return content
            else:
                print(f"  API error (attempt {attempt+1}): {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            print(f"  API call failed (attempt {attempt+1}): {e}")

        if attempt < max_retries - 1:
            wait = 2 ** attempt
            print(f"  Retrying in {wait}s...")
            time.sleep(wait)

    return None


def parse_response(content: str, expected_count: int) -> list | None:
    """Parse LLM response as JSON array of verdicts."""
    if not content:
        return None

    # Try direct JSON parse
    content = content.strip()
    # Remove markdown code fences if present
    if content.startswith("```"):
        content = re.sub(r'^```(?:json)?\s*', '', content)
        content = re.sub(r'\s*```$', '', content)

    try:
        verdicts = json.loads(content)
    except json.JSONDecodeError:
        # Try to find JSON array in the response
        match = re.search(r'\[[\s\S]*\]', content)
        if match:
            try:
                verdicts = json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
        else:
            return None

    if not isinstance(verdicts, list):
        return None

    # Validate each entry
    for v in verdicts:
        if not isinstance(v, dict):
            return None
        if "index" not in v or "verdict" not in v:
            return None
        v["verdict"] = v["verdict"].lower()
        if v["verdict"] not in ("pass", "skip"):
            return None

    # Check count
    if len(verdicts) < expected_count:
        print(f"  Warning: got {len(verdicts)} verdicts, expected {expected_count}")
    elif len(verdicts) > expected_count:
        verdicts = verdicts[:expected_count]

    return verdicts


BATCH_SIZE = 80  # max items per LLM call (keep prompt+response under token limits)


def filter_batch(batch: list, source: str, batch_num: int, total_batches: int) -> list | None:
    """Filter one batch of items. Returns verdicts list or None."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_filter_prompt(batch)},
    ]
    print(f"  Batch {batch_num}/{total_batches} ({len(batch)} items)...")
    content = call_llm(messages)
    if not content:
        print(f"  ✗ API call failed for batch {batch_num}")
        return None
    return parse_response(content, len(batch))


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Filter collected items using LLM API")
    parser.add_argument("source", choices=["rss", "blogs", "consulting", "thinktank"],
                        help="Source to filter")
    parser.add_argument("--date", default=None,
                        help="Date string (YYYY-MM-DD), defaults to today")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be filtered without calling API")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE,
                        help=f"Items per LLM call (default: {BATCH_SIZE})")
    args = parser.parse_args()

    date_str = args.date or time.strftime("%Y-%m-%d")
    source = args.source

    raw_path = TMP_DIR / source / f"{source}_raw_{date_str}.json"
    output_path = TMP_DIR / source / f"{source}_filtered_{date_str}.json"

    if not raw_path.exists():
        print(f"No raw data for {source} on {date_str}")
        return 0

    with open(raw_path) as f:
        items = json.load(f)

    if not items:
        print(f"Raw data for {source} on {date_str} is empty")
        return 0

    print(f"Filtering {len(items)} items from {source} ({date_str})...")

    if args.dry_run:
        print("DRY RUN: would call LLM API")
        for i, item in enumerate(items):
            title = item.get("title", "?")[:60]
            print(f"  [{i}] {title}")
        return 0

    # Split into batches
    batch_size = args.batch_size
    all_verdicts = []
    total_batches = (len(items) + batch_size - 1) // batch_size

    for batch_num in range(total_batches):
        start = batch_num * batch_size
        end = min(start + batch_size, len(items))
        batch = items[start:end]

        verdicts = filter_batch(batch, source, batch_num + 1, total_batches)
        if verdicts is None:
            if all_verdicts:
                # Partial success — use what we have
                print(f"  ⚠️ Batch {batch_num + 1} failed, using {len(all_verdicts)} partial verdicts")
            else:
                print(f"  ✗ All batches failed for {source}")
                return 1
            break
        all_verdicts.extend(verdicts)

    # Write results
    with open(output_path, "w") as f:
        json.dump(all_verdicts, f, indent=2, ensure_ascii=False)

    pass_count = sum(1 for v in all_verdicts if v.get("verdict") == "pass")
    skip_count = sum(1 for v in all_verdicts if v.get("verdict") == "skip")
    print(f"  ✓ Results: {pass_count} pass, {skip_count} skip (from {len(all_verdicts)} verdicts)")
    print(f"  ✓ Saved to {output_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
