#!/usr/bin/env python3
"""
Medium 分支（英文）：采集素材 → LLM 英文观点写作 → 创建飞书文档。

零依赖 Agent，纯 Python。

Flow:
  1. 读取采集素材
  2. LLM 英文写作：观点犀利、判断清晰、有数据支撑
  3. 创建飞书文档（含 title + body）
  4. 输出文档链接

Usage:
  python3 write_medium_article.py [--date YYYY-MM-DD] [--dry-run]
"""

import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

from lib.llm import call_model
from lib.models import get_model

# ── Media-specific political filter ──
# For Medium (English, global audience), we need a broader filter.
# This is a subset — explicitly flag content that could cause issues.
SENSITIVE_PATTERNS = [
    # Geopolitical / military / national security
    ("china", "military"), ("china", "warfare"), ("china", "cyber"),
    ("china", "censor"), ("china", "political"), ("china", "propaganda"),
    ("china", "authoritarian"), ("china", "human rights"), ("china", "ccp"),
    ("china", "communist"), ("china", "government"), ("china", "defense"),
    ("china", "security"), ("china", "regulat"),
    # Direct red flags
    "chinese electronic warfare", "china's electronic warfare",
    "chinese military", "china military",
    "political correctness china", "china political correctness",
    # Geography / territorial
    "taiwan", "xinjiang", "tibet", "hong kong",
]


def _is_sensitive_topic(material: dict) -> bool:
    """Check if material touches sensitive topics for Medium publication."""
    url = (material.get("url", "") or "").lower()
    title = (material.get("title", "") or "").lower()
    content = (material.get("content", "") or "").lower()[:2000]
    topic = (material.get("topic", "") or "").lower()
    text_to_check = f"{url} {title} {topic} {content}"

    for pattern in SENSITIVE_PATTERNS:
        if isinstance(pattern, tuple):
            term1, term2 = pattern
            for i in range(0, len(text_to_check), 500):
                segment = text_to_check[i:i + 1000]
                if term1 in segment and term2 in segment:
                    print(f"    ✗ Filtered (compound '{term1}' + '{term2}'): {url[:60]}...")
                    return True
        else:
            if pattern in text_to_check:
                print(f"    ✗ Filtered (term '{pattern}'): {url[:60]}...")
                return True
    return False


def filter_sensitive(materials: list[dict]) -> list[dict]:
    """Filter out sensitive materials."""
    filtered = [m for m in materials if not _is_sensitive_topic(m)]
    removed = len(materials) - len(filtered)
    if removed:
        print(f"  Filtered {removed} sensitive material(s)")
    return filtered


# ── Paths ──
WORKSPACE_DIR = Path(__file__).resolve().parent
COLLECTOR_DIR = WORKSPACE_DIR / "collector"
REPORTS_DIR = WORKSPACE_DIR / "reports"
ALL_URLS_FILE = REPORTS_DIR / "_index" / "all_urls.tsv"
OUTPUT_BASE = WORKSPACE_DIR / "medium-articles"

from lib.materials import (
    get_all_collected_urls,
    read_material_content,
    sample_materials,
    filter_used_materials,
)

# ── English system prompt for Medium ──

MEDIUM_SYSTEM_PROMPT = """You are a sharp, opinionated tech writer for Medium. Your articles are the kind people screenshot and share because they say something uncomfortable but true.

## Voice & Tone

- **Direct, not academic.** Write like you're explaining something to a smart colleague over coffee.
- **Have a thesis.** Every article needs ONE clear, arguable take — not a survey of opinions.
- **Be specific.** Vague generalities get scrolled past. Data, names, numbers, dates — anchor every claim.
- **No hedge words.** Don't "might", "could", "it seems". Commit. If you're uncertain about something, say "I'm not sure about X, but here's what I do know: Y."
- **No corporate speak.** No "leverage", "paradigm shift", "ecosystem", "synergy", "stakeholder alignment". Write like a human.
- **Short paragraphs.** 2-4 sentences max. Medium readers scan. Make every line earn its place.
- **Provocative but honest.** You're allowed to be spicy, but you must be fair. Don't strawman the opposing view.

## Article Structure

Don't follow a template. Each article's structure should serve its thesis. But in general:

1. **Open with the tension.** Start with a concrete scene, a surprising data point, or a contrarian claim. Hook in the first 3 sentences.
2. **State your thesis fast.** By paragraph 4, the reader should know exactly what you're arguing.
3. **Build the case.** Layer your evidence: data → mechanism → implication. Each section advances the argument.
4. **Acknowledge the counterargument.** Show you understand why someone might disagree — then explain why you still hold your position.
5. **End with a punch.** A memorable line, a challenge, a question that lingers. Don't summarize — escalate.

## What NOT to do

- Don't write "In this article, I will explore..." — just explore it.
- Don't start with dictionary definitions or rhetorical questions.
- Don't end with "Only time will tell" — that's not a conclusion.
- Don't pad with fluff. If a sentence can be deleted without losing meaning, delete it.
- Don't use images/placeholders — this is pure text output.

## Writing Process (execute in your head, output only the article)

Your internal steps (not output):
1. Read the materials and identify the ONE most interesting signal
2. Determine why this matters (the mechanism, not just the fact)
3. Find the edge — what's counterintuitive or underappreciated?
4. Test your thesis against counterarguments
5. Write tight

## Output Format

Output ONLY the article. No thinking steps, no intermediate notes.

```
# The Title — Make It Count

Article body in Markdown. Use **bold** for emphasis, `code` for technical terms.

---

*End with a line that makes people want to share.*
```

Language: English (US).
Length: 800-2000 words. Tight is better.
"""


# ── Helpers ──

def slugify(text: str) -> str:
    """Slugify text for directory naming."""
    slug = re.sub(r'[^\w\-]', '-', text.lower())
    slug = re.sub(r'-+', '-', slug).strip('-')
    return slug[:80]


def build_material_context(materials: list[dict]) -> str:
    """Build context string from materials for the LLM."""
    parts = []
    for i, m in enumerate(materials, 1):
        content = m.get("content", "")
        url = m.get("url", "")
        key_signal = m.get("key_signal", "")
        header = f"=== Source {i} ===\nURL: {url}"
        if key_signal:
            header += f"\nCore thesis: {key_signal}"
        header += f"\n{content[:4000]}"
        parts.append(header)
    return "\n\n".join(parts)


def extract_article_from_response(response: str) -> Optional[str]:
    """Extract the Markdown article from LLM response."""
    if response.startswith("#"):
        return response
    code_block = re.search(r'```(?:markdown|md)?\n(.*?)\n```', response, re.DOTALL)
    if code_block:
        return code_block.group(1).strip()
    for pattern in [
        r'(?:Here is|Here\'s|Below is|This is)(?: the)?(?: final)?(?: article|post)(?: in full)?[\-:]*\s*\n*(.*)',
        r'---+\n*(.*?)$',
    ]:
        match = re.search(pattern, response, re.DOTALL)
        if match:
            content = match.group(1).strip()
            if content.startswith("#") or len(content) > 500:
                return content
    lines = response.split("\n")
    article_start = 0
    for i, line in enumerate(lines):
        if line.startswith("# "):
            article_start = i
            break
    if article_start > 0:
        return "\n".join(lines[article_start:])
    return response


DIGEST_PATTERN = re.compile(r'^摘要[：:](.+)$', re.MULTILINE)


def extract_digest(response: str) -> str:
    """Extract digest/tagline line from response."""
    # Medium articles don't use "摘要:" — try common English patterns
    for pattern in [
        r'^Tagline[：:]\s*(.+)$',
        r'^TL;DR[：:]\s*(.+)$',
        r'^The takeaway[：:]\s*(.+)$',
    ]:
        match = re.search(pattern, response, re.MULTILINE | re.IGNORECASE)
        if match:
            return match.group(1).strip()[:120]
    return ""


def strip_digest_from_article(response: str) -> str:
    """Remove tagline/digest line from article before saving."""
    lines = response.split("\n")
    cleaned = []
    for line in lines:
        if re.match(r'^(Tagline|TL;DR|The takeaway)[：:]\s', line, re.IGNORECASE):
            continue
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def _create_feishu_doc(title: str, body: str) -> Optional[str]:
    """Create a Feishu document with the article content.
    
    Uses feishu_doc tool via the agent runtime (JSON command to stdout).
    Falls back to lark-cli if available.
    
    Returns doc_token on success, None on failure.
    """
    # Method 1: Try lark-cli (most reliable from Python)
    # lark-cli docs +create requires: title, content as stdin
    import subprocess as _sp
    
    # Build full markdown content
    full_content = f"# {title}\n\n{body}"
    
    try:
        # Try lark-cli first
        result = _sp.run(
            ["lark-cli", "docs", "+create", "--title", title, "--markdown", "-"],
            input=full_content,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            # Parse output for doc URL or token
            stdout = result.stdout or ""
            # lark-cli typically outputs "Created: https://.../docx/TOKEN"
            import re as _re
            # Try to find URL or token in output
            for line in stdout.split("\n"):
                line = line.strip()
                # Look for URL pattern
                url_match = _re.search(r'https://[^/]+/[a-z]+/([a-zA-Z0-9]+)', line)
                if url_match:
                    return url_match.group(1)
                # Or direct token
                if len(line) == 32 and line.isalnum():
                    return line
            # If we got here but return code was 0, check for JSON output
            try:
                data = json.loads(stdout)
                if "data" in data and "document" in data["data"]:
                    return data["data"]["document"]["document_id"]
                if "document_id" in data:
                    return data["document_id"]
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
            # Last resort: return whatever we got
            if stdout:
                print(f"  lark-cli output: {stdout[:200]}")
                return stdout.strip()
    except FileNotFoundError:
        print(f"  lark-cli not found, cannot create Feishu doc")
    except _sp.TimeoutExpired:
        print(f"  lark-cli timed out (60s)")
    except Exception as e:
        print(f"  lark-cli error: {e}")
    
    return None


# ── Writing pipeline ──

def write_medium_article(
    date_str: str,
    dry_run: bool = False,
    max_materials: int = 5,
    model: str = None,
    materials_override: Optional[list[dict]] = None,
) -> Optional[str]:
    """
    Medium article writing pipeline.

    Outputs to: medium-articles/YYYY-MM-DD-title/article.md

    Returns output directory path on success, None on failure.
    """
    print(f"\n{'='*60}")
    print(f"✍️  Medium Article Pipeline — {date_str}")
    print(f"{'='*60}")

    if materials_override:
        print("\n1️⃣  Using provided materials...")
        enriched = []
        for item in materials_override:
            normalized_item = dict(item)
            if "content" not in normalized_item and normalized_item.get("content_text"):
                normalized_item["content"] = normalized_item["content_text"]
            enriched.append(normalized_item)
        print(f"  Loaded {len(enriched)} materials")

        enriched = filter_used_materials(enriched)
        if not enriched:
            print("  ❌ All materials have been used before")
            return None
    else:
        print("\n1️⃣  Reading collected materials...")
        collected = get_all_collected_urls()
        if not collected:
            print("  ❌ No collected materials found")
            return None
        print(f"  Found {len(collected)} items")

        print("\n2️⃣  Reading content...")
        enriched = []
        for m in collected:
            content = read_material_content(m["url"])
            if content:
                m["content"] = content
                enriched.append(m)
                print(f"  ✓ {m['url'][:70]}... ({len(content)} chars)")

        if not enriched:
            print("  ❌ Could not read any material content")
            return None

        print("\n3️⃣  Filtering sensitive content...")
        enriched = filter_sensitive(enriched)
        if not enriched:
            print("  ❌ All materials filtered out")
            return None

    print("\n4️⃣  Sampling materials...")
    sampled = sample_materials(enriched, max_materials)
    print(f"  Selected {len(sampled)} materials")

    if not sampled:
        print("  ❌ No materials survived filtering. Aborting.")
        return None

    MIN_CONTENT_CHARS = 500
    for s in sampled:
        content = s.get("content", "") or ""
        print(f"    📎 {s['url'][:70]} ({len(content)} chars)")
        if len(content) < MIN_CONTENT_CHARS:
            print(f"  ⚠️ Material '{s['url'][:50]}' has < {MIN_CONTENT_CHARS} chars")

    print(f"\n5️⃣  Writing article with LLM...")
    context = build_material_context(sampled)

    # Weighted prompt: emphasize sharp opinion
    user_prompt = f"""Write a sharp, opinionated Medium article based on the source materials below.

## Requirements

1. **ONE strong thesis** — not a survey. Pick the most interesting angle from the materials and commit.
2. **Data-driven** — use specific numbers, names, dates from the sources. Every paragraph should advance the argument.
3. **Honest about uncertainty** — don't fake confidence in speculative areas, but be decisive on what IS knowable.
4. **Counterargument-aware** — briefly acknowledge why someone might disagree, then explain why you hold your view anyway.
5. **Shareable ending** — the last line should be quotable. Something people want to put in their bio or tweet.

## Sources

{context}

## Output

Output ONLY the final article in Markdown. If you want, include a tagline after the title like:

```
# The Real Reason Enterprise AI Keeps Failing

Tagline: It's not the tech. It's that nobody asked "why" before they started.
```

Then the article body. No thinking steps, no outlines, no notes.

Language: English (US).
Length: 800-2000 words. Tight is better.
"""

    messages = [
        {"role": "system", "content": MEDIUM_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    response = call_model(messages, temperature=0.9, max_tokens=4096, model=model)
    if not response:
        print("  ❌ Model returned no response")
        return None

    article = extract_article_from_response(response)
    if not article:
        print("  ❌ Could not extract article from response")
        return None

    print(f"  ✓ Article generated: {len(article)} chars")

    # Extract title
    title_match = re.search(r'^#\s+(.+)$', article, re.MULTILINE)
    if title_match:
        title = title_match.group(1).strip()
    else:
        title = f"medium-article-{date_str}"
        article = f"# {title}\n\n{article}"

    title_slug = slugify(title)
    output_dir = OUTPUT_BASE / f"{date_str}-{title_slug}"

    if dry_run:
        print(f"\n  [Dry run] Would write to: {output_dir}")
        print(f"\n  Preview:\n{article[:500]}...")
        return str(output_dir)

    output_dir.mkdir(parents=True, exist_ok=True)

    # Save article body locally for reference
    article_body = strip_digest_from_article(article)
    output_dir.mkdir(parents=True, exist_ok=True)
    article_path = output_dir / "article.md"
    article_path.write_text(article_body, encoding="utf-8")
    print(f"  ✓ Article saved locally: {article_path}")

    # Extract tagline
    digest = extract_digest(response)
    if not digest:
        digest = extract_digest(article)
    meta = {
        "title": title,
        "tagline": digest,
        "date": date_str,
        "platform": "feishu-doc",
        "source_urls": [s["url"] for s in sampled],
    }
    meta_path = output_dir / "meta.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    if digest:
        print(f"  ✓ Tagline: {digest}")
    else:
        print(f"  ⚠️ No tagline extracted")

    # Step: Create Feishu document
    print("\n6️⃣  Creating Feishu document...")
    doc_token = _create_feishu_doc(title, article_body)
    if doc_token:
        doc_url = f"https://www.feishu.cn/docx/{doc_token}"
        print(f"  ✅ Feishu doc created: {doc_url}")
        # Save doc info
        meta["feishu_doc_token"] = doc_token
        meta["feishu_doc_url"] = doc_url
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        print(f"  ⚠️ Feishu doc creation failed, article saved locally only")

    print(f"\n  ✅ Output: {output_dir}/")
    print(f"     📄 article.md (local copy)")
    if digest:
        print(f"     🏷️  {digest}")
    return str(output_dir)


def main():
    parser = argparse.ArgumentParser(
        description="Medium: write English op-ed article → create Feishu doc"
    )
    parser.add_argument("--date", type=str, default=None,
                        help="Date string (YYYY-MM-DD), defaults to today")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be done without writing anything")
    parser.add_argument("--max-materials", type=int, default=5,
                        help="Maximum materials to include in context")
    parser.add_argument("--model", type=str, default=get_model("writing"),
                        help="Writing model. Default: openai/gpt-5.4")
    parser.add_argument("--materials", type=str, default=None,
                        help="Path to JSON file with pre-loaded materials")
    args = parser.parse_args()

    date_str = args.date or datetime.date.today().isoformat()

    materials_override = None
    if args.materials:
        mp = Path(args.materials)
        if mp.exists():
            materials_override = json.loads(mp.read_text(encoding="utf-8"))
            print(f"📦 Loaded {len(materials_override)} materials from {mp}")
        else:
            print(f"  ⚠️ Materials file not found: {mp}")
            return 1

    output_dir = write_medium_article(
        date_str,
        dry_run=args.dry_run,
        max_materials=args.max_materials,
        model=args.model,
        materials_override=materials_override,
    )

    if output_dir:
        print(f"\n{'='*60}")
        print(f"✅ Medium article pipeline complete!")
        print(f"   Output: {output_dir}")
        print(f"{'='*60}")
        # Print feishu doc URL if available
        meta_file = Path(output_dir) / "meta.json"
        if meta_file.exists():
            try:
                meta_data = json.loads(meta_file.read_text(encoding="utf-8"))
                if "feishu_doc_url" in meta_data:
                    print(f"\n📄 Feishu Doc: {meta_data['feishu_doc_url']}")
            except Exception:
                pass
        return 0
    else:
        print(f"\n{'='*60}")
        print(f"❌ Medium article pipeline failed")
        print(f"{'='*60}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
