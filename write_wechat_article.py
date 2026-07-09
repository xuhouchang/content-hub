#!/usr/bin/env python3
"""
微信分支（中文）：采集素材 → LLM中文写作 → 配图 → 输出。

零依赖 Agent，纯 Python 管线。

Flow:
  1. 采集素材
  2. LLM 中文写作（DeepSeek Chat → 润色）
  3. 图片搜索 + 嵌入图片
  4. 输出到 wechat-articles/YYYY-MM-DD-title/
  5. 写入 pending_articles.json（异步发布队列）

Usage:
  python3 write_wechat_article.py [--date YYYY-MM-DD] [--dry-run]
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

# ── Import LLM and materials from lib/ ──
from lib.llm import call_model
from lib.models import get_model

# ── Polish script reference ──（移到 COLLECTOR_DIR 定义之后）
from lib.materials import (
    get_all_collected_urls,
    read_material_content,
    sample_materials,
    filter_used_materials,
)

# ── Chinese political topic filter ──
# Keywords that indicate the content touches Chinese politics.
# Articles matching these (in URL, title, topic, or content)
# are excluded from the writing pipeline.
#
# NOTE: Use compound patterns to avoid false positives.
# "governance" alone is too broad (catches OpenAI/Anthropic safety content).
# Only flag when both China-context AND sensitive topic appear.
CHINA_POLITICS_PATTERNS = [
    # China + specific domain
    ("china", "military"),
    ("china", "warfare"),
    ("china", "cyber"),
    ("china", "censor"),
    ("china", "political"),
    ("china", "propaganda"),
    ("china", "authoritarian"),
    ("china", "human rights"),
    ("china", "ccp"),
    ("china", "communist"),
    ("china", "regulation"),
    ("china", "regulator"),
    ("china", "government"),
    ("china", "defense"),
    ("china", "security"),
    # Direct political terms (these alone are specific enough)
    "chinese electronic warfare",
    "china's electronic warfare",
    "chinese military",
    "china military",
    "political correctness china",
    "china political correctness",
    # Geography
    "taiwan",
    "xinjiang",
    "tibet",
    "hong kong",
]

from lib import slugify


def _is_china_political_topic(material: dict) -> bool:
    """Check if a material touches Chinese political topics.
    
    Uses compound patterns (e.g., "china" + "military") to avoid
    false positives on generic AI governance / safety content.
    """
    url = (material.get("url", "") or "").lower()
    title = (material.get("title", "") or "").lower()
    content = (material.get("content", "") or "").lower()[:2000]
    topic = (material.get("topic", "") or "").lower()
    
    text_to_check = f"{url} {title} {topic} {content}"
    
    for pattern in CHINA_POLITICS_PATTERNS:
        if isinstance(pattern, tuple):
            # Compound pattern: both terms must appear
            term1, term2 = pattern
            # Ensure term1 and term2 are close-ish (within 500 chars)
            for i in range(0, len(text_to_check), 500):
                segment = text_to_check[i:i+1000]
                if term1 in segment and term2 in segment:
                    print(f"    ✗ Filtered (compound '{term1}' + '{term2}'): {url[:60]}...")
                    return True
        else:
            # Simple string match
            if pattern in text_to_check:
                # For single-term geo names, mark; for others, already specific enough
                print(f"    ✗ Filtered (term '{pattern}'): {url[:60]}...")
                return True
    return False


def filter_china_political(materials: list[dict]) -> list[dict]:
    """Filter out materials related to Chinese political topics."""
    filtered = [m for m in materials if not _is_china_political_topic(m)]
    removed = len(materials) - len(filtered)
    if removed:
        print(f"  Filtered {removed} China-political material(s)")
    return filtered

COLLECTOR_DIR = Path(__file__).parent
WORKSPACE_DIR = COLLECTOR_DIR.parent
REPORTS_DIR = WORKSPACE_DIR / "reports"
ALL_URLS_FILE = REPORTS_DIR / "_index" / "all_urls.tsv"
OUTPUT_BASE = WORKSPACE_DIR / "wechat-articles"
IMAGE_MATCHER_SCRIPT = COLLECTOR_DIR / "image_search.py"
EMBED_IMAGES_SCRIPT = COLLECTOR_DIR / "embed_images.py"
POLISH_SCRIPT = COLLECTOR_DIR / "polish_article.py"

# ── System prompt for article writing ──

WRITING_SYSTEM_PROMPT = """你是一位公众号撰稿人。素材给了你什么，你就写什么。

## 绝对禁区

严禁讨论中国的时事政治、政策、政府、监管或审查等话题。就当没看到。

你的文章不能是政治评论。

读者看完后至少会说一句"有意思"，而不是"就这？"。

## 写作方向

素材是什么就写什么。不强行塞一个"反常识结论"。

- 如果素材是一篇论文，就把它的方法、数据、结论写清楚
- 如果素材是一个案例，就把前因后果、得失讲透
- 如果素材是一个趋势判断，就把传导链条说明白
- 如果素材是某个人的观点，就把他的核心论证展开

不必每篇追求"颠覆认知"。有些好文章就是"把这个事情说明白了"。

排除项：模型/产品发布动态、技术教程、工具评测、投融资新闻。

## 写作目标（两条路径通用）
1. 让读者理解一个正在发生的变化
2. 解释变化背后的机制
3. 说明变化意味着什么
4. 主动处理一个常见误读或边界
5. 留下一个可复述的判断

## 工作流程（必须在脑中执行，不得输出到文章中）

**这是你的内部思考步骤，不是文章大纲。你的输出只有最终的完整文章，不包含任何思考过程和中间产出。**

在脑中严格按顺序执行以下步骤，但只输出第 9 步的结果（完整文章）：

1. 路径判定：判断当前素材适合路径 A 还是 B。
2. 信息抽取：提取核心事实、数据、案例、结论。
3. 异常信号识别：找 2-4 个"咦"信号。
4. 三层萃取：表层结论→中层机制→深层趋势。
5. 核心判断生成：生成 2-4 个候选判断（淘汰无机制、无边界、可套用的）。
6. 情报扫描：搜索已有讨论和反方观点。
7. 反证与边界检查：找反证，明确适用边界。
8. 推理链设计：设计递进推理链（有证据等级）。
9. 正文写作：选择章节结构，按下面的写作要求展开。
10. 配图：在正文中插入图片占位符。

### 正文写作
选择以下结构之一：异常解释型、趋势传导型、失败复盘型、机制拆解型、观点校准型。

写作要求：
- 每一节开头先给判断，再给解释
- 不要按原报告目录写
- 不要大段复述材料
- 不要堆概念
- 不要使用「不是……而是……」这种话术结构。反例：❌"企业的问题不是技术不足，而是组织没跟上"→ ✅"组织跟不上是真正的瓶颈"
- 禁止段落/章节结尾使用「这意味着什么」「这说明」「这告诉我们」等总结型收束。结尾留一个可复述的判断，不额外总结。
- 不要使用咨询腔套话："赋能""抓手""闭环生态""新范式"
- 不要把所有问题都归因于"企业应该拥抱 AI"（路径A专用）
- 多写机制、传导、边界、代价
- 少写愿景、趋势、口号、宏大判断
- 文章应该有克制感：判断强，但表达不浮夸
- 路径B：保留悬念感，逐步揭示，让读者"一个接一个地发现有意思的东西"
- **章节标题必须是面向读者的好标题，不是框架术语。比如用"为什么你买的 AI 工具没用上？"代替"正文写作"或"反常识信号"。**

每一段写完后自检：
- 这一段是在复述材料，还是在推进自己的推理？
- 如果删掉这一段，文章的判断链会断吗？
- 读者读到这一段，会觉得"有意思"还是"这我知道"？

### 配图占位符
在正文中用 Markdown 图片格式 `![图片描述](./images/image-NNN-xxx.jpeg)` 插入占位符，每800-1000字至少插一张。放在关键数据出现时、对比/趋势出现时、章节核心结论出现时、框架/流程被描述时。

**格式要求：**
- 使用 `![描述文本](./images/image-NNN-xxx.jpeg)` 格式，N 从 001 开始递增
- 不要使用 `<!-- IMAGE: N -->` 注释格式（已废弃）
- 描述文本用中文，概括该图应传达的视觉内容，后续会用这个文本做配图搜索

## 标题规则
硬性要求（必须同时满足）：
1. 包含一个明确的判断或结论，不能只是话题标签
2. 让读者产生"这跟我有关"或"这有意思"的代入感
3. 不能照抄报告原标题

选配要素（至少命中两项）：
- 反直觉结论、数据冲击、冲突感、紧迫感、场景代入、下一个问题、好奇驱动

避雷清单（绝对禁止）：
- 《XX报告解读》《XX深度分析》——没有信息量
- 《AI的发展趋势》《数字化转型的思考》——太泛
- 《关于XX的几点思考》——80年代论文标题
- 《赋能…》《开启…新篇章》——公众号营销腔
- 《重磅！…》——标题党
- 纯疑问句且答案显而易见
- 照抄报告原标题

## 中文写作规范

- 全文使用中文标点符号
- 英文术语、缩写保留原文不翻译
- 禁止章节标题使用内部框架术语
- 禁止在文章正文中出现"ABCD级证据""推理链标记"等内部流程残留

## 风格指引
- 不用"赋能""抓手""闭环""新范式"等套话
- 不用"这意味着什么""这说明""这告诉我们"总结收束
- 不用"不是……而是……"话术结构
- 段落开头可以直给判断，也可以先叙事再出结论——看素材最适合什么节奏
- 标题要有信息量，不能只是话题标签。可以有"为什么""怎么办""真相是"，也可以用陈述句直说结论

## 输出格式

输出只有最终的完整文章，不包含任何推理过程、思考步骤、框架术语、内部标记。

格式：
```
# 标题

摘要: 一句话，最多50字

文章正文...(含 `![描述](./images/image-NNN-xxx.jpeg)` 图片占位符)

```

输出语言：中文（中国大陆，简体）。
文章长度：2000-3500字。

摘要用陈述句直接说结论，不要以"本文"或"这篇文章"开头。
"""


# ── Helpers ──

def build_material_context(materials: list[dict]) -> str:
    """Build a context string from materials for the LLM.
    
    Includes key_signal (观点信号) when available, so the LLM can
    see each material's core thesis at a glance before reading the full content.
    """
    parts = []
    for i, m in enumerate(materials, 1):
        content = m.get("content", "")
        url = m.get("url", "")
        key_signal = m.get("key_signal", "")
        header = f"=== 素材 {i} ===\n来源: {url}"
        if key_signal:
            header += f"\n核心观点: {key_signal}"
        header += f"\n{content[:3000]}"
        parts.append(header)
    return "\n\n".join(parts)


def extract_article_from_response(response: str) -> Optional[str]:
    """Extract the Markdown article from LLM response."""
    # Try to find Markdown with a heading
    if response.startswith("#"):
        return response

    # Try to find content between markdown code blocks
    code_block = re.search(r'```(?:markdown|md)?\n(.*?)\n```', response, re.DOTALL)
    if code_block:
        return code_block.group(1).strip()

    # Try to find content after "以下是文章" or similar pattern
    for pattern in [
        r'(?:以下是|这是|最终)?(?:的)?(?:完整)?(?:文章|正文)[：:]\s*\n*(.*)',
        r'——+(.*?)$',
    ]:
        match = re.search(pattern, response, re.DOTALL)
        if match:
            content = match.group(1).strip()
            if content.startswith("#") or len(content) > 1000:
                return content

    # Fallback: return everything after the last instruction-like paragraph
    lines = response.split("\n")
    article_start = 0
    for i, line in enumerate(lines):
        if line.startswith("# "):
            article_start = i
            break
    if article_start > 0:
        return "\n".join(lines[article_start:])

    return response  # Best effort


DIGEST_PATTERN = re.compile(r'^摘要[：:](.+)$', re.MULTILINE)


def extract_digest(response: str) -> str:
    """Extract digest line from the LLM response (from "摘要: xxx")."""
    match = DIGEST_PATTERN.search(response)
    if match:
        return match.group(1).strip()[:80]
    return ""


def _enqueue_for_publish(output_dir: str) -> None:
    """Write output_dir to pending_articles.json for the async publish cron."""
    pending_file = WORKSPACE_DIR / "pending_articles.json"
    try:
        queue = []
        if pending_file.exists():
            queue = json.loads(pending_file.read_text(encoding="utf-8"))
        if output_dir not in queue:
            queue.append(output_dir)
        pending_file.write_text(json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  ✅ Enqueued {output_dir} for async publish (queue size: {len(queue)})")
    except Exception as e:
        print(f"  ⚠️ Failed to enqueue for publish: {e}")


# ── Recent article topic tracking ──
RECENT_TOPICS_FILE = OUTPUT_BASE / "_recent_topics.json"


def _log_article_topic(
    title: str,
    digest: str,
    output_dir: Path,
    source_urls: list[str],
    cluster_ids: list[str] | None = None,
    mark_source_urls: bool = True,
):
    """Log article title+digest to a JSON file for diversity tracking.
    The sample_materials function reads this to avoid topic repetition.

    Optionally marks each source_url as 'used' in all_urls.tsv so it won't
    be selected again by get_all_collected_urls().
    """
    try:
        RECENT_TOPICS_FILE.parent.mkdir(parents=True, exist_ok=True)
        entries = []
        if RECENT_TOPICS_FILE.exists():
            entries = json.loads(RECENT_TOPICS_FILE.read_text(encoding="utf-8"))
        entries.append({
            "title": title,
            "digest": digest,
            "date": datetime.date.today().isoformat(),
            "output_dir": str(output_dir),
            "source_urls": source_urls[:5],
            "cluster_ids": cluster_ids or [],
        })
        # Keep only last 30 entries
        entries = entries[-30:]
        RECENT_TOPICS_FILE.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")

        # ── Also mark source URLs as used in all_urls.tsv ──
        marked = 0
        added = 0
        tsv_path = REPORTS_DIR / "_index" / "all_urls.tsv"
        if mark_source_urls and source_urls:
            tsv_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Read existing content (empty file if doesn't exist)
            content = ""
            if tsv_path.exists():
                content = tsv_path.read_text(encoding="utf-8")
            
            lines_changed = False
            matched_urls = set()
            for url in source_urls:
                url_raw = url.strip()  # keep original (with possible trailing slash)
                
                # Check if URL exists in TSV (handle both with/without trailing slash)
                import re as _re
                # Match URL optionally followed by /, then tab
                url_pattern = _re.escape(url_raw.rstrip("/")) + r"/?"
                pattern = _re.compile(
                    r"^(" + url_pattern + r")\t(\S+)(\t.*)?$",
                    re.MULTILINE
                )
                new_content, count = pattern.subn(
                    lambda m: (m.group(1).rstrip("/") or url_raw.rstrip("/")) + "\tused" + (m.group(3) or ""),
                    content
                )
                if count > 0:
                    content = new_content
                    marked += count
                    matched_urls.add(url_raw.rstrip("/"))
                    lines_changed = True
                else:
                    # URL not in TSV — append with 'used' status
                    today_str = datetime.date.today().isoformat()
                    content += f"{url_raw.rstrip('/')}\tused\t{today_str}\n"
                    added += 1
                    lines_changed = True
            
            if lines_changed:
                tsv_path.write_text(content, encoding="utf-8")
                if marked:
                    print(f"  🏷️  Marked {marked} source URLs as used in all_urls.tsv")
                if added:
                    print(f"  ➕ Added {added} new URL(s) as used in all_urls.tsv")
    except Exception as e:
        print(f"  ⚠️ Failed to log article topic: {e}")


def _load_recent_articles() -> list[dict]:
    """Load the last N recently written articles for angle-diversity checking.
    Returns list of dicts with title, digest, date.
    """
    try:
        if RECENT_TOPICS_FILE.exists():
            entries = json.loads(RECENT_TOPICS_FILE.read_text(encoding="utf-8", errors="replace"))
            if isinstance(entries, list):
                return entries[-6:]  # last 6 articles
            elif isinstance(entries, dict) and "articles" in entries:
                return entries["articles"][-6:]
    except Exception:
        pass
    return []


def strip_digest_from_article(response: str) -> str:
    """Remove the digest line from article content before saving."""
    return re.sub(r'^.*摘要[：:].*$\n?', '', response, count=1, flags=re.MULTILINE).strip()


def count_image_placeholders(content: str) -> int:
    """Count image placeholders in content. Supports both formats."""
    # Primary: <!-- IMAGE: N --> (legacy support)
    count1 = len(re.findall(r'<!--\s*IMAGE\s*:\s*\d+\s*-->', content, re.IGNORECASE))
    # Secondary: ![desc](./images/image-NNN-xxx.jpeg)
    count2 = len(re.findall(r'!\[[^\]]*\]\(\./images/image-\d{3}-', content))
    # Prefer ![]() count if any found; fall back to legacy
    return count2 if count2 > 0 else count1


def run_image_search(query: str, output_dir: str, index: int = 1) -> bool:
    """Run image_search.py for one query. Returns True if at least one image downloaded."""
    if not IMAGE_MATCHER_SCRIPT.exists():
        print(f"  ⚠️ image_search.py not found: {IMAGE_MATCHER_SCRIPT}")
        return False

    cmd = [
        sys.executable, str(IMAGE_MATCHER_SCRIPT),
        query,
        output_dir,
        "--index", str(index),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            # Check if image file was created
            import glob
            image_files = glob.glob(os.path.join(output_dir, f"image-{index:03d}-*"))
            return len(image_files) > 0
        if result.stderr:
            print(f"    ⚠️ {result.stderr.strip()}")
        return False
    except subprocess.TimeoutExpired:
        print(f"  ⚠️ image_search.py timed out")
        return False


def generate_image_search_queries(article_text: str, num_placeholders: int) -> list[str]:
    """Generate English search queries for image matching based on article content."""
    # Extract key themes from article
    queries = []

    # Common enterprise AI themes
    keyword_maps = {
        "判断力": ["decision making bottleneck", "executive judgment", "business decisions"],
        "瓶颈": ["bottleneck business", "workflow constraint", "process limitation"],
        "效率": ["productivity team", "efficiency workplace", "business performance"],
        "组织": ["organization structure", "team collaboration", "corporate culture"],
        "流程": ["business workflow", "process automation", "workflow optimization"],
        "协作": ["team collaboration", "human collaboration", "meeting discussion"],
        "数据": ["data analytics", "business data", "data visualization"],
        "AI": ["artificial intelligence enterprise", "AI business", "AI workplace"],
        "Agent": ["AI agent automation", "autonomous system", "AI workflow"],
        "管理": ["management strategy", "leadership decision", "executive meeting"],
    }

    for keyword, search_terms in keyword_maps.items():
        if keyword in article_text:
            queries.extend(search_terms)
            if len(queries) >= num_placeholders * 2:
                break

    # Fallback to generic queries if nothing matched
    if not queries:
        queries = [
            "enterprise AI digital transformation",
            "business strategy meeting",
            "team workplace technology",
            "organization change management",
        ]

    return queries[:num_placeholders + 1]


def write_article(
    date_str: str,
    dry_run: bool = False,
    max_materials: int = 5,
    model: str = None,
    materials_override: Optional[list[dict]] = None,
) -> Optional[str]:
    """
    Main article writing pipeline.

    Args:
        materials_override: Optional pre-loaded materials list.
            Each item: {"url": str, "content": str}.
            When provided, skips collection from all_urls.tsv / saved files.
            Used by the daily case-study pipeline where an agent hand-picks
            and web-searches materials before calling the writing pipeline.

    Returns the output directory path on success, None on failure.
    """
    print(f"\n{'='*60}")
    print(f"📝 Article Pipeline — {date_str}")
    print(f"{'='*60}")

    if materials_override:
        # Bypass all_urls.tsv reading — materials already provided by agent
        print("\n1️⃣  Using agent-provided materials (--materials override)...")
        enriched = []
        for item in materials_override:
            normalized_item = dict(item)
            if "content" not in normalized_item and normalized_item.get("content_text"):
                normalized_item["content"] = normalized_item["content_text"]
            enriched.append(normalized_item)
        print(f"  Loaded {len(enriched)} materials from override")

        # ⛔ CODE-LEVEL FILTER: Remove materials already marked 'used'
        from lib.materials import filter_used_materials
        enriched = filter_used_materials(enriched)
        if not enriched:
            print("  ❌ All override materials have been used before")
            return None
    else:
        # Step 1: Get collected materials
        print("\n1️⃣  Reading collected materials...")
        collected = get_all_collected_urls()
        if not collected:
            print("  ❌ No collected materials found in all_urls.tsv")
            return None

        print(f"  Found {len(collected)} collected items")

        # Step 2: Read content for each material
        print("\n2️⃣  Reading material content...")
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

        # Step 3: Filter out China-political materials before sampling
        print("\n3️⃣  Filtering materials...")
        enriched = filter_china_political(enriched)
        if not enriched:
            print("  ❌ All materials filtered out due to China-political content")
            return None

    # Step 4: Sample materials for LLM
    print("\n4️⃣  Sampling materials for writing...")
    sampled = sample_materials(enriched, max_materials)
    print(f"  Selected {len(sampled)} materials")

    # ⛔ STRONG GUARD: If no materials were sampled after all filtering, abort.
    # Prevents articles from being hallucinated without any real source content.
    if not sampled:
        print("  ❌ No materials survived filtering. Aborting: cannot write article")
        print("     without at least one source material to draw from.")
        return None

    # === STRONG GUARD: Verify all sampled materials have substantive content ===
    MIN_CONTENT_CHARS = 500
    poor_materials = []
    for s in sampled:
        content = s.get("content", "") or ""
        print(f"    📎 {s['url'][:70]} ({len(content)} chars)")
        if len(content) < MIN_CONTENT_CHARS:
            poor_materials.append(s)

    if poor_materials:
        print(f"  ⚠️  {len(poor_materials)}/{len(sampled)} materials have <{MIN_CONTENT_CHARS} chars content!")
        print(f"  Attempting to replace them with better-content alternatives...")
        # Find replacements from enriched pool (not already sampled, content >= MIN_CONTENT_CHARS)
        sampled_urls = {s["url"] for s in sampled}
        replacements = [m for m in enriched if m["url"] not in sampled_urls and len(m.get("content", "") or "") >= MIN_CONTENT_CHARS]
        replacements.sort(key=lambda m: len(m.get("content", "") or ""), reverse=True)

        new_sampled = []
        for s in sampled:
            if s["url"] in {p["url"] for p in poor_materials} and replacements:
                replacement = replacements.pop(0)
                print(f"    🔄 Replacing low-content ({len(s.get('content',''))}c) material:")
                print(f"       ✗ {s['url'][:70]}")
                print(f"       ✓ {replacement['url'][:70]} ({len(replacement.get('content',''))} chars)")
                new_sampled.append(replacement)
            else:
                new_sampled.append(s)
        sampled = new_sampled

        # Final check: reinforce poor materials with web_fetch if still below threshold
        final_poor = [s for s in sampled if len(s.get("content", "") or "") < MIN_CONTENT_CHARS]
        if final_poor:
            print(f"  ❌ {len(final_poor)} materials still lack content after replacement:")
            for fp in final_poor:
                print(f"     {fp['url'][:70]} ({len(fp.get('content',''))} chars)")
            print(f"  Aborting: cannot write article without real content to draw from.")
            return None

    print(f"  ✅ All {len(sampled)} materials have substantive content (≥{MIN_CONTENT_CHARS} chars each)")

    # Step 5: Load recent topics and check for angle diversity
    print("\n5️⃣  Checking angle diversity...")
    recent_articles = _load_recent_articles()
    if recent_articles:
        print(f"  📋 Found {len(recent_articles)} recently written articles for angle-avoidance")
        for ra in recent_articles:
            print(f"     • 《{ra['title'][:40]}》 — {ra.get('digest', '')[:50]}")
        
        # -- Hard dedup: filter out materials whose source URLs were used in recent articles
        recent_used_urls = set()
        for ra in recent_articles:
            used_urls = ra.get("source_urls", [])
            for u in used_urls:
                recent_used_urls.add(u.rstrip("/").lower())
        
        if recent_used_urls and sampled:
            dupe_urls_found = set()
            clean_sampled = []
            for s in sampled:
                s_url = (s.get("url", "") or "").rstrip("/").lower()
                if s_url in recent_used_urls:
                    print(f"     🔄 Removing recently-used source: {s_url[:70]}")
                    dupe_urls_found.add(s_url)
                else:
                    clean_sampled.append(s)
            
            replaced_count = len(sampled) - len(clean_sampled)
            if replaced_count > 0:
                # Backfill from the broader enriched pool (excluding used URLs)
                sampled_urls_set = {s.get("url", "") for s in sampled}
                backfill_candidates = [
                    m for m in enriched
                    if m.get("url", "") not in sampled_urls_set
                    and (m.get("url", "") or "").rstrip("/").lower() not in recent_used_urls
                    and len(m.get("content", "") or "") >= MIN_CONTENT_CHARS
                ]
                backfill_candidates.sort(key=lambda m: len(m.get("content", "") or ""), reverse=True)
                
                for dup_url in list(dupe_urls_found)[:replaced_count]:
                    if backfill_candidates:
                        r = backfill_candidates.pop(0)
                        print(f"       ✓ Replaced with: {r.get('url', '')[:70]}")
                        clean_sampled.append(r)
                
                sampled = clean_sampled
                print(f"  ✅ Replaced {replaced_count} material(s) from recently-used sources")
            else:
                print(f"  ✅ No source-URL overlap with recent articles")
        else:
            print(f"  ✅ No source-URL overlap with recent articles")
    else:
        print("  📋 No recent articles found for angle check")

    # Step 6: Build context and call LLM
    print("\n6️⃣  Calling OpenAI (GPT-5.4 via OpenRouter) for article writing...")
    context = build_material_context(sampled)

    # Build angle-avoidance instruction from recent articles
    angle_instruction = ""
    if recent_articles:
        # 提取近一周文章的核心主题标签（从标题和摘要中自动推断）
        recent_themes = [
            "AI治理/透明度/可解释性",
            "员工对AI的信任/无声抵抗",
            "企业组织结构变革",
            "人机协作/任务分配",
            "AI选型/模型对比",
            "AI Agent编排/多智能体",
            "ROI/投入产出/效率度量",
            "AI落地失败/落地障碍",
            "AI与人才/技能/岗位变化",
            "AI研究/模型行为/反常识发现",
        ]
        
        # 从近期文章标题+摘要中自动推断主题标签
        used_themes = set()
        governance_keywords = ["解释","透明","治理","信任","说服","合法","合规","安慰剂","障眼法","可解释"]
        organization_keywords = ["组织","团队","流程","结构","流程","变革","再造","分工"]
        agent_keywords = ["Agent","智能体","编排","多代理","自主","自动化"]
        trust_keywords = ["信任","抵抗","不用","沉默","抗拒","恐惧"]
        roi_keywords = ["ROI","效率","人效","产出","成本","增长"]
        
        for ra in recent_articles:
            text = (ra.get("title", "") + " " + ra.get("digest", "")).lower()
            if any(kw in text for kw in governance_keywords):
                used_themes.add("AI治理/透明度/可解释性")
            if any(kw in text for kw in trust_keywords):
                used_themes.add("员工信任/采纳/抵抗")
            if any(kw in text for kw in organization_keywords):
                used_themes.add("企业组织变革")
            if any(kw in text for kw in agent_keywords):
                used_themes.add("AI Agent/自动化")
            if any(kw in text for kw in roi_keywords):
                used_themes.add("ROI/效率度量")
        
        angle_instruction = (
            "⚠️ 近7天已发文章主题标签（必须完全避开）：\n"
        )
        if used_themes:
            angle_instruction += (
                "以下主题在过去7天内已被写过，**绝对禁止再次使用**:\n"
            )
            for theme in sorted(used_themes):
                angle_instruction += f"  - ❌ {theme}\n"
        else:
            angle_instruction += "（无已复用主题记录）\n"
        
        angle_instruction += (
            "\n---\n"
            "近7天已发文章（对你要求：**不要写跟这些文章角度、推理链、核心判断类似的内容。**）：\n"
        )
        for i, ra in enumerate(recent_articles):
            date_str = f"（{ra['date']}）" if ra.get("date") else ""
            angle_instruction += f"{i+1}. 《{ra['title']}》 — {ra.get('digest', '')} {date_str}\n"
        angle_instruction += (
            "\n**自我校验步骤**（在脑中完成，不出现在输出中）：\n"
            "1. 这篇文章的核心理念或判断，和上面哪一篇类似？如果有，必须换。\n"
            "2. 你的标题、叙事角度、核心数据来源是否与上面任何一篇重叠超过60%？如果是，必须换。\n"
            "3. 如果你觉得'但我不一样的点是...'，那意味着你在擦边——必须换。\n"
            "4. 只有当你确定'这个角度上面没有任何一篇文章触碰过'时，才是安全的。\n"
        )

    user_prompt = f"""以下是我从信息源收集到的素材内容。请按照你的写作流程执行：

1. 先判定这是路径 A（企业落地导向）还是路径 B（有意思的 AI 研究发现）
2. 分析素材中的异常信号
3. 做三层萃取（表层/中层/深层）
4. 生成2-4个候选核心判断
5. 做情报扫描、反证与边界检查
6. 设计推理链 — 确保推理链与最近已发文章有实质性区别
7. 撰写完整正文（用 Markdown 图片格式 `![描述](./images/image-NNN-xxx.jpeg)` 插入占位符）
8. 直接输出最终文章，不需要输出中间的推理过程。文章正文中禁止出现 T1/T2/T3/T4、ABC 分级标记、或其他推理过程的显式标签。

{angle_instruction}素材内容：
{context}

注意：直接输出完整的 Markdown 文章，以 # 标题开头。如果素材让你想到的判断与最近文章类似，一定要主动换方向。

⚠️ 强制要求：你必须从以上素材中提取具体的数据、案例、判断来支撑文章论点。如果素材中没有对应的信息，不要自行杜撰数据或虚构案例。文章中必须有至少 2 处引用素材中的具体信息。"""
    messages = [
        {"role": "system", "content": WRITING_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    response = call_model(messages, temperature=1.0, max_tokens=8192, model=model)
    if not response:
        print(f"  ❌ Model returned no response")
        return None

    article = extract_article_from_response(response)
    if not article:
        print("  ❌ Could not extract article from response")
        print(f"  Response preview: {response[:500]}")
        return None

    print(f"  ✓ Article generated: {len(article)} chars")

    # === STRONG GUARD: Verify article references specific material content ===
    # Check that the article isn't just generic LLM output
    material_urls = [s["url"] for s in sampled]
    material_content = " ".join([s.get("content", "")[:2000] for s in sampled])
    
    # Extract key phrases/numbers from material content to verify they appear in article
    import re as _re
    number_pattern = _re.findall(r'\d+%|\d+\s*[万万亿千亿]|\d+\.\d+', material_content)
    specific_phrases = [p for p in number_pattern if len(p) > 2][:5]  # e.g. "73%", "80%"
    
    found_refs = 0
    for phrase in specific_phrases:
        if phrase in article:
            found_refs += 1
    
    if found_refs < 1 and specific_phrases:
        print(f"  ⚠️  Warning: Article contains only {found_refs}/{len(specific_phrases)} key data points from materials")
        print(f"      Key data from materials: {specific_phrases[:5]}")
        print(f"      Proceeding anyway (may be a qualitative article without numbers)")

    # Extract title for directory naming
    title_match = re.search(r'^#\s+(.+)$', article, re.MULTILINE)
    if title_match:
        title = title_match.group(1).strip()
    else:
        # Fallback: generate title from digest or first sentence
        digest = extract_digest(response)
        if digest:
            # Truncate digest to safe title length
            title = digest[:60].rstrip("。，. ,")
            print(f"  ⚠️ No # title found, fallback to digest: {title}")
        else:
            title = "untitled"
        # Prepend title to article so polish/publish scripts can find it
        article = f"# {title}\n\n{article}"
        response = article
    title_slug = slugify(title)

    output_dir = OUTPUT_BASE / f"{date_str}-{title_slug}"
    images_dir = output_dir / "images"

    if dry_run:
        print(f"\n  [Dry run] Would write to: {output_dir}")
        print(f"\n  Article preview:\n{article[:500]}...")
        return str(output_dir)

    # Create output directories
    output_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)

    # Write article (strip digest line from body)
    digest = extract_digest(response)
    article_body = strip_digest_from_article(article)
    article_path = output_dir / "article.md"
    article_path.write_text(article_body, encoding="utf-8")
    print(f"  ✓ Article saved: {article_path}")

    # Write digest as separate metadata file
    if digest:
        meta_path = output_dir / "meta.json"
        import json
        meta_path.write_text(json.dumps({"digest": digest}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  ✓ Digest: {digest}")
    else:
        print(f"  ⚠️ No digest extracted (LLM didn't output '摘要:' line)")

    # ── Log article topic for diversity tracking ──
    # Collect cluster_ids from sampled materials for dedup tracking
    sampled_cluster_ids = list({
        s.get("dedup", {}).get("cluster_id", "")
        for s in sampled
        if s.get("dedup", {}).get("cluster_id")
    })
    _log_article_topic(
        title,
        digest,
        output_dir,
        [s["url"] for s in sampled],
        cluster_ids=sampled_cluster_ids,
        mark_source_urls=not bool(materials_override),
    )

    # Step 6: Polish article with OpenAI via OpenRouter
    # Done BEFORE image matching — text must be finalized first
    print("\n6️⃣  Polishing article...")
    if POLISH_SCRIPT.exists():
        cmd = [
            sys.executable, str(POLISH_SCRIPT),
            str(article_path),
            "--model", get_model("writing"),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            for line in result.stdout.strip().split("\n"):
                print(f"  {line}")
            if result.returncode != 0:
                print(f"  ⚠️ Polish script returned non-zero: {result.returncode}")
                if result.stderr:
                    print(f"  stderr: {result.stderr[:300]}")
        except subprocess.TimeoutExpired:
            print(f"  ⚠️ polish_article.py timed out (180s), continuing without polish")
    else:
        print(f"  ⚠️ polish_article.py not found, skipping polish")

    # Re-read article after polish
    article = article_path.read_text(encoding="utf-8")

    # ── Hard post-processing: clean AI-typical phrasing ──
    print("\n🔧 Running hard post-processing on article text...")
    from lib.llm import call_model as _call_llm
    
    def _hard_clean_article(text: str) -> str:
        """One-pass hard regex post-processing to scrub AI-typical phrasing.
        Applied AFTER LLM polish, as a safety net.
        """
        changes = []
        original = text

        # 1. Replace "不是……而是……" pattern
        #    "不能X，而是Y" + similar structures
        count_before = text.count("不是")
        def _replace_not_but(m):
            prefix = m.group(1)
            after = m.group(2).strip()
            if prefix.rstrip().endswith('这') and not after.startswith('是'):
                return prefix.rstrip() + '是' + after
            if prefix.rstrip() and not prefix.rstrip()[-1] in '这那的' and not after.startswith('是'):
                return prefix.rstrip() + '是' + after
            return prefix.rstrip() + after
        text = re.sub(
            r'(.+?)不是[^，。]{2,60}?(?:，|,)\s*而(?:不是)?(.+)',
            _replace_not_but,
            text
        )
        count_after = text.count("不是")
        if count_before > count_after:
            changes.append(f"cleaned {count_before - count_after} '不是…而是…' instance(s)")

        # 2. Kill section-ending summary headers like "## 这意味着什么"
        text = re.sub(
            r'^#{1,3}\s*这意味着什么[：:]*\s*$\n?',
            '',
            text,
            flags=re.MULTILINE
        )

        # 3. Kill standalone summary-sentence lines
        text = re.sub(
            r'^(?:这意味着|这告诉我们|这说明|这里的启示是|这背后的启示)\s[^。\n]*[。]?$\n?',
            '',
            text,
            flags=re.MULTILINE
        )

        # 4. Strip orphan blank lines left after removals
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = text.strip() + '\n'

        changed = text != original
        if changed:
            print(f"  \U0001f527 Post-process: {'; '.join(changes) if changes else 'text modified'}")
        else:
            print(f"  \u2705 Post-process: no changes needed")
        return text

    article = _hard_clean_article(article)

    # Write cleaned article back
    article_path.write_text(article, encoding="utf-8")

    # Step 7: Match images (after text is finalized)
    print("\n7️⃣  Matching images...")
    num_placeholders = count_image_placeholders(article)
    print(f"  Found {num_placeholders} image placeholders")

    if num_placeholders > 0:
        queries = generate_image_search_queries(article, num_placeholders)
        for i, query in enumerate(queries[:num_placeholders + 1], 1):
            print(f"  Searching image {i}/{min(len(queries), num_placeholders + 1)}: '{query}'...")
            success = run_image_search(query, str(images_dir), i)
            if success:
                print(f"    ✓ Images downloaded")
            else:
                print(f"    ⚠️ No images found for this query")
    else:
        print("  ⚠️ No placeholders found, adding default placeholders")
        # Add placeholders every ~1000 chars
        paragraphs = article.split("\n\n")
        new_article = []
        img_idx = 1
        char_count = 0
        for para in paragraphs:
            new_article.append(para)
            char_count += len(para)
            if char_count > 800 and img_idx <= 4:
                new_article.append(f"\n![配图{img_idx}](./images/image-{img_idx:03d}-placeholder.jpeg)\n")
                img_idx += 1
                char_count = 0
        article = "\n\n".join(new_article)
        article_path.write_text(article, encoding="utf-8")

        # Search default images
        default_queries = [
            "enterprise AI digital transformation",
            "business team collaboration",
            "organization workflow",
            "decision making strategy",
        ]
        for i, query in enumerate(default_queries[:4], 1):
            print(f"  Searching image {i}: '{query}'...")
            run_image_search(query, str(images_dir), i)

    # Step 8: Embed images
    print("\n8️⃣  Embedding images...")
    if EMBED_IMAGES_SCRIPT.exists():
        cmd = [
            sys.executable, str(EMBED_IMAGES_SCRIPT),
            str(article_path),
            str(images_dir),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            print(result.stdout)
            if result.stderr:
                print(f"  stderr: {result.stderr}")
        except subprocess.TimeoutExpired:
            print(f"  ⚠️ embed_images.py timed out")
    else:
        print(f"  ⚠️ embed_images.py not found: {EMBED_IMAGES_SCRIPT}")

    # Step 9: Write to pending_articles.json for async publish cron
    print("\n9️⃣  Enqueuing for async publish...")
    _enqueue_for_publish(str(output_dir))

    # List output
    print(f"\n  ✅ Output: {output_dir}/")
    for f in sorted(output_dir.iterdir()):
        if f.is_dir():
            print(f"     📁 images/ ({len(list(f.iterdir()))} files)")
        else:
            print(f"     📄 {f.name}")

    return str(output_dir)


def main():
    parser = argparse.ArgumentParser(description="WeChat: write Chinese article + image matching (no publish)")
    parser.add_argument("--date", type=str, default=None,
                        help="Date string (YYYY-MM-DD), defaults to today")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be done without writing anything")
    parser.add_argument("--max-materials", type=int, default=5,
                        help="Maximum materials to include in context")
    parser.add_argument("--model", type=str, default=get_model("writing"),
                        help="Writing model. Default: openai/gpt-5.4 (OpenRouter)")
    parser.add_argument("--materials", type=str, default=None,
                        help="Path to JSON file with pre-loaded materials. "
                             "Each item: {\"url\": \"...\", \"content\": \"...\"}. "
                             "When set, skips all_urls.tsv collection.")
    args = parser.parse_args()

    date_str = args.date or datetime.date.today().isoformat()

    materials_override = None
    if args.materials:
        import json as json_mod
        mp = Path(args.materials)
        if mp.exists():
            materials_override = json_mod.loads(mp.read_text(encoding="utf-8"))
            print(f"📦 Loaded {len(materials_override)} materials from {mp}")
        else:
            print(f"  ⚠️ Materials file not found: {mp}")

    output_dir = write_article(date_str, dry_run=args.dry_run, max_materials=args.max_materials, model=args.model, materials_override=materials_override)

    if output_dir:
        print(f"\n{'='*60}")
        print(f"✅ WeChat article pipeline complete!")
        print(f"   Output: {output_dir}")
        print(f"   Images embedded; pending async publish via 07:30 cron")
        print(f"{'='*60}")
        return 0
    else:
        print(f"\n{'='*60}")
        print(f"❌ WeChat article pipeline failed")
        print(f"{'='*60}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
