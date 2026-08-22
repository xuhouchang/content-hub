#!/usr/bin/env python3
"""
Weekly Synthesis — 替代 weekly-synthesis skill。

读取过去7天素材 → 主题聚类 → 生成提纲 → 写全文 → 配图 → 发布。

Usage:
  python3 synthesize_weekly.py --step outline [--date YYYY-MM-DD] [--dry-run]
  python3 synthesize_weekly.py --step write --topic-idx N [--date YYYY-MM-DD] [--dry-run] [--model MODEL]
  python3 synthesize_weekly.py --step publish --topic-idx N [--date YYYY-MM-DD]
  python3 synthesize_weekly.py --step list
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

# ── Auto-load .env from collector/ directory ──
COLLECTOR_DIR = Path(__file__).parent
dotenv_path = COLLECTOR_DIR / ".env"
if dotenv_path.exists():
    with open(dotenv_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            if value and not os.environ.get(key):
                os.environ[key] = value

from lib.llm import call_model
from lib.models import get_model
from lib.materials import read_material_content
from lib import slugify

# ── paths ──
WORKSPACE_DIR = COLLECTOR_DIR.parent
REPORTS_DIR = WORKSPACE_DIR / "reports"
WEEKLY_DIR = WORKSPACE_DIR / "wechat-drafts" / "weekly"

# Scripts
IMAGE_SEARCH = COLLECTOR_DIR / "image_search.py"
EMBED_IMAGES = COLLECTOR_DIR / "embed_images.py"
WECHAT_PUBLISH = COLLECTOR_DIR / "wechat_publish.py"

OUTLINES_FILE = WEEKLY_DIR / "outlines.json"

# ── Import system prompts ──
# Try library_system_prompts.py first, fall back to hardcoded string
try:
    from library_system_prompts import WEEKLY_SYNTHESIS_SYSTEM_PROMPT
except ImportError:
    WEEKLY_SYNTHESIS_SYSTEM_PROMPT = """你是一个深度写作引擎。你的任务是将过去7天采集的多篇素材进行主题聚类、整合编译，写成面向中国企业管理者的深度公众号文章。

## 与单篇写作的区别
- 单篇写作：基于1-2篇素材做推理延伸
- 周度整合（本 prompt）：基于过去7天所有素材做主题交叉验证和多源整合
- 核心：多篇素材找到共同趋势，而不是拼凑多篇素材的摘要

## 选题筛选标准
1. 优先选择有2篇以上素材交叉验证、且锚定「企业AI应用落地」的主题
2. 同一现象/趋势被不同来源提到 → 优先
3. 有数据支撑的互证观点 → 优先（落地证据：真实工作场景/流程改造/岗位变化/业务结果/实施数据/可复现步骤）
4. 不同角度的同一问题分析 → 优先
5. 纯产品发布/模型评测 → 排除
6. 缺乏企业视角的纯学术讨论 → 排除
7. 纯治理/风险/安全/对齐话题，若通篇只是框架/政策而无企业落地证据 → 低优先，不作选题主轴
8. 只有一篇素材且无交叉验证 → 排除

## 写作要求
1. 开头从具体现象/问题切入
2. 中间多源交叉验证，展示不同角度的观点
3. 结尾收束到企业可操作的启示
4. 视角锚定「AI 时代的企业落地」
5. 交叉引用原则：
   - 同一个观点有多个来源 → 合并呈现，标注多源
   - 不同来源有冲突观点 → 并列呈现，写明分歧
   - 一个来源提供核心框架，其他来源补充数据 → 框架为主线，数据嵌入
6. 素材引用格式：在正文中用 [来源: 文章标题] 标注
7. 长度：3000-5000 字
8. 标题不能写成"多篇报告汇总"的感觉，必须提炼出一个独立判断

## 推理链设计（必须先做）
1. 一句话回答核心判断
2. 拆解推理步骤 T1 → T2 → T3 → ...
3. 检查递进感：去掉 T1，读者能猜出前文？
4. 素材入链：把多篇素材打散，分配给每个推理步骤

## 标题规则
硬性要求：包含明确判断、有代入感、不照抄素材标题
避雷清单：报告解读 / 发展思考 / 几点思考 / 纯疑问句

## 输出格式
- 第1步（主题聚类）输出 JSON
- 第2步（写全文）输出完整 Markdown（含 <!-- IMAGE: N --> 占位符）
"""


# ── Helpers ──

def get_materials_from_last_7_days(date_str: str = None) -> list[dict]:
    """
    Get materials collected in the last 7 days.
    
    Scans reports/ directory tree for *.md files with date-like prefixes
    matching the last 7 days. Falls back to all_urls.tsv if directory scan
    yields nothing.
    """
    today = datetime.date.today() if not date_str else datetime.date.fromisoformat(date_str)
    date_range = [today - datetime.timedelta(days=i) for i in range(7)]
    date_prefixes = {d.isoformat() for d in date_range}  # e.g. {"2026-05-13", ...}
    date_month_prefixes = {d.strftime("%Y-%m") for d in date_range}  # e.g. {"2026-05", ...}

    items = []

    # Strategy 1: Scan all_report directories for *.md files with matching date prefix
    scanned_urls = set()
    for md_file in REPORTS_DIR.rglob("*.md"):
        # Skip _index/ files
        if "_index" in md_file.parts:
            continue

        filename = md_file.name
        # Check if filename starts with a date in our range
        # Expected pattern: YYYY-MM-DD_rest-of-title.md
        date_match = re.match(r'^(\d{4}-\d{2}-\d{2})_', filename)
        if date_match:
            file_date = date_match.group(1)
            if file_date in date_prefixes:
                url = extract_url_from_md(md_file)
                title = extract_title_from_md(md_file)
                display = title or filename
                items.append({
                    "url": url or str(md_file),
                    "line": display,
                    "file_path": str(md_file),
                    "date": file_date,
                })
                if url:
                    scanned_urls.add(url)

    # Strategy 2: Scan all_urls.tsv for items not already captured by file scan
    all_urls_file = REPORTS_DIR / "_index" / "all_urls.tsv"
    if all_urls_file.exists():
        with open(all_urls_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split("\t")
                if len(parts) >= 2 and parts[-1] == "collected":
                    url = parts[0].strip()
                    if url and url not in scanned_urls:
                        # Check if this URL's report file exists in date range
                        file_path = find_report_file(url)
                        if file_path:
                            # Check if the file's date matches
                            filename = file_path.name
                            date_match = re.match(r'^(\d{4}-\d{2}-\d{2})_', filename)
                            if date_match and date_match.group(1) in date_prefixes:
                                items.append({
                                    "url": url,
                                    "line": parts[0],
                                    "file_path": str(file_path),
                                    "date": date_match.group(1),
                                })

    # Deduplicate by URL
    seen_urls = set()
    unique_items = []
    for item in items:
        url = item.get("url", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            unique_items.append(item)

    # Sort by date descending (newest first)
    unique_items.sort(key=lambda x: x.get("date", ""), reverse=True)

    print(f"  Found {len(unique_items)} materials from last 7 days")
    return unique_items


def extract_url_from_md(filepath: Path) -> Optional[str]:
    """Extract source URL from a report Markdown file."""
    try:
        content = filepath.read_text(encoding="utf-8", errors="replace")
        # Look for URL pattern in content
        url_match = re.search(r'https?://[^\s\n)\]]+(?:[^\s\n)\]]|"(?=\s|$)|"(?=[\n\r]))+', content)
        return url_match.group(0) if url_match else None
    except Exception:
        return None


def extract_title_from_md(filepath: Path) -> Optional[str]:
    """Extract title from a report Markdown file (first # heading)."""
    try:
        content = filepath.read_text(encoding="utf-8", errors="replace")
        title_match = re.search(r'^#\s+(.+)$', content, re.MULTILINE)
        return title_match.group(1).strip() if title_match else None
    except Exception:
        return None


def find_report_file(url: str) -> Optional[Path]:
    """Search reports/ for a Markdown file containing this URL."""
    url_stripped = url.strip().rstrip("/")
    for md_file in REPORTS_DIR.rglob("*.md"):
        if "_index" in md_file.parts:
            continue
        try:
            content = md_file.read_text(encoding="utf-8", errors="replace")
            if url_stripped in content or url in content:
                return md_file
        except Exception:
            continue
    return None


def read_material_by_path(file_path: str) -> Optional[str]:
    """Read material content from a file path."""
    path = Path(file_path)
    if not path.exists():
        return None
    content = path.read_text(encoding="utf-8", errors="replace")
    # Remove front matter
    content = re.sub(r'^---\n.*?\n---\n', '', content, flags=re.DOTALL)
    # Remove trailing metadata
    content = re.sub(r'\n---\n\n\*Collected by.*?\*$', '', content, flags=re.DOTALL)
    return content.strip()


# ── Pipeline Steps ──

def step_outline(date_str: str = None, model: str = None) -> list[dict]:
    """Step 1: Topic clustering → outline generation."""
    print(f"\n{'='*60}")
    print(f"📋 Weekly Synthesis — Outline — {date_str or datetime.date.today().isoformat()}")
    print(f"{'='*60}")

    # 1. Get materials from last 7 days
    print("\n1️⃣  Scanning materials from last 7 days...")
    materials = get_materials_from_last_7_days(date_str)

    if not materials:
        print("  No materials found in last 7 days.")
        return []

    # 2. Read content for each material
    print("\n2️⃣  Reading material content...")
    enriched = []
    for m in materials[:30]:  # max 30 materials
        url = m.get("url", "")
        title = m.get("line", url)[:80]
        file_path = m.get("file_path", "")

        # Try reading by file path first, then by URL
        content = None
        if file_path:
            content = read_material_by_path(file_path)
        if not content and url:
            content = read_material_content(url)

        if content:
            enriched.append({
                "url": url,
                "title": title,
                "content": content[:6000],  # cap per-material context
                "date": m.get("date", ""),
            })
            print(f"  ✓ {title[:60]} ({len(content)} chars)")
        else:
            enriched.append({
                "url": url,
                "title": title,
                "content": "",
                "date": m.get("date", ""),
            })
            print(f"  ⚠️  No saved content: {title[:60]}")

    if not enriched:
        print("  No readable materials.")
        return []

    # 3. If too many materials, do a first-pass filter
    if len(enriched) > 15:
        print(f"\n  Too many materials ({len(enriched)}). First-pass filter...")
        enriched = _filter_top_materials(enriched, model=model or get_model("synthesis"))

    # 4. Call LLM for topic clustering
    print(f"\n3️⃣  Topic clustering (LLM)...")

    materials_text = ""
    for i, item in enumerate(enriched):
        if item["content"]:
            materials_text += f"\n--- Material {i+1} ---\nURL: {item['url']}\n{item['content'][:5000]}\n"
        else:
            materials_text += f"\n--- Material {i+1} ---\nURL: {item['url']}\n(no content available)\n"

    messages = [
        {"role": "system", "content": WEEKLY_SYNTHESIS_SYSTEM_PROMPT},
        {"role": "user", "content": f"""你今天需要做主题聚类和选题判断。以下是过去7天采集的素材：

{materials_text[:100000]}

请执行：
1. 快速阅读所有素材
2. 找出有交叉验证潜力的主题（2篇以上素材指向同一趋势）
3. 生成至少2个候选提纲主题（最多3个）

输出格式为纯 JSON：
{{
  "top_topics": [
    {{
      "title": "文章标题（暂定）",
      "reason": "为什么选这个主题（一句话）",
      "confidence": "high/medium/low",
      "sources": [
        {{"name": "来源简称", "contribution": "这篇贡献了什么核心观点"}}
      ],
      "structure": {{
        "introduction": "开头怎么切入",
        "sections": [
          {{"title": "小节标题", "content": "核心论点"}},
          {{"title": "小节标题", "content": "核心论点"}}
        ],
        "conclusion": "结尾判断方向"
      }},
      "digest": "一句话摘要（15-20字）"
    }}
  ]
}}

如果素材不足以支撑任何主题，返回 {{"top_topics": []}}。
只输出 JSON，不要其他文字。"""},
    ]

    response = call_model(messages, temperature=0.7, max_tokens=4096, model=model)
    if not response:
        print("  ❌ Model returned no response")
        return []

    # Parse JSON from response
    try:
        json_match = re.search(r'\{.*\}', response, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group())
        else:
            data = json.loads(response)
        topics = data.get("top_topics", [])
    except (json.JSONDecodeError, AttributeError):
        print("  ⚠️  Could not parse response as JSON")
        print(f"  Raw preview: {response[:500]}")
        return []

    if not topics:
        print("  No viable topics identified.")
        return []

    # 5. Save outlines
    WEEKLY_DIR.mkdir(parents=True, exist_ok=True)

    outlines = {
        "date": date_str or datetime.date.today().isoformat(),
        "material_count": len(enriched),
        "topics": topics,
    }
    with open(OUTLINES_FILE, "w", encoding="utf-8") as f:
        json.dump(outlines, f, indent=2, ensure_ascii=False)

    print(f"\n  ✅ {len(topics)} topics identified and saved to outlines.json")
    for t in topics:
        print(f"     [{t.get('confidence', '?')}] {t['title']}")
        print(f"            ↳ {t.get('reason', '')[:80]}")

    return topics


def _filter_top_materials(materials: list[dict], model: str = None) -> list[dict]:
    """When >15 materials, let LLM pick the most relevant 15."""
    summary_text = ""
    for i, m in enumerate(materials, 1):
        title = m.get("title", "?")
        url = m.get("url", "")
        content_preview = m.get("content", "")[:300]
        summary_text += f"[{i}] {title}\n  URL: {url}\n  Preview: {content_preview}...\n\n"

    messages = [
        {"role": "system", "content": "你是一个素材筛选助手。从列表中选出最值得关注的15篇（或更少）。本号主轴是「企业AI应用落地」。优先选择包含**真实工作场景/业务流程改造/岗位变化/业务结果（ROI、降本、提效、自动化率）/实施数据/可复现步骤**的素材（无论来源是工程、产品还是案例）。组织/管理变革作为辅助，纯治理/风险/安全/对齐话题（若无落地证据）排到最后。返回 JSON 格式的索引列表。"},
        {"role": "user", "content": f"""素材列表：

{summary_text[:50000]}

请选出最相关的 15 篇（或更少，如果不足15篇则全选），按相关性从高到低排序。
只输出 JSON: {{"selected_indices": [1, 3, 5, ...]}}"""},
    ]

    response = call_model(messages, temperature=0.3, max_tokens=1024, model=model)
    if not response:
        print("  ⚠️  Filter LLM returned nothing, using all materials")
        return materials[:15]

    try:
        json_match = re.search(r'\{.*\}', response, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group())
        else:
            data = json.loads(response)
        indices = sorted(set(data.get("selected_indices", [])))
        selected = [materials[i - 1] for i in indices if 1 <= i <= len(materials)]
        print(f"  → Filtered to {len(selected)} materials (from {len(materials)})")
        return selected
    except (json.JSONDecodeError, AttributeError, IndexError):
        print(f"  ⚠️  Could not parse filter response, using first 15")
        return materials[:15]


def step_write(topic_idx: int = 0, date_str: str = None,
               model: str = None, dry_run: bool = False) -> Optional[dict]:
    """Step 2: Write full article for the selected topic."""
    print(f"\n{'='*60}")
    print(f"✍️  Writing article for topic {topic_idx}")
    print(f"{'='*60}")

    # 1. Load outlines
    if not OUTLINES_FILE.exists():
        print("  ❌ No outlines.json found. Run --step outline first.")
        return None

    with open(OUTLINES_FILE, "r", encoding="utf-8") as f:
        outlines = json.load(f)

    topics = outlines.get("topics", [])
    if topic_idx >= len(topics):
        print(f"  ❌ Topic index {topic_idx} out of range (max {len(topics) - 1})")
        return None

    topic = topics[topic_idx]

    # 2. Build source materials context
    print("\n1️⃣  Loading source materials...")
    materials_text = ""
    for s in topic.get("sources", []):
        name = s.get("name", "?")
        contribution = s.get("contribution", "")
        materials_text += f"- {name}: {contribution}\n"

    structure_text = json.dumps(topic.get("structure", {}), indent=2, ensure_ascii=False)

    # 3. Generate article
    print(f"\n2️⃣  Writing article...")
    print(f"  Title: {topic['title']}")
    print(f"  Model: {model or get_model('synthesis')}")

    if dry_run:
        print("  (dry-run, skip LLM call)")
        return None

    title = topic["title"]
    digest = topic.get("digest", "")

    messages = [
        {"role": "system", "content": WEEKLY_SYNTHESIS_SYSTEM_PROMPT},
        {"role": "user", "content": f"""请根据以下主题和素材，写一篇完整的公众号文章。

## 主题
{title}

## 选题理由
{topic.get('reason', '')}

## 素材来源
{materials_text}

## 建议结构
{structure_text}

## 写作要求
1. 不要拼凑素材——核心是推理链递进
2. 素材引用格式：[来源: 来源名称]
3. 每800-1000字插入一个 <!-- IMAGE: N --> 占位符
4. 3000-5000字
5. 结尾收束到可操作的判断

请输出完整的 Markdown 文章。"""},
    ]

    response = call_model(messages, temperature=0.7, max_tokens=None, model=model)
    if not response:
        print("  ❌ No response from model")
        return None

    # 4. Save article
    slug = slugify(title[:40])
    date_prefix = date_str or datetime.date.today().isoformat()
    output_dir = WEEKLY_DIR / f"{date_prefix}-{slug}"
    output_dir.mkdir(parents=True, exist_ok=True)

    article_path = output_dir / "article.md"
    article_path.write_text(response.strip(), encoding="utf-8")

    # Save metadata
    meta = {
        "title": title,
        "digest": digest,
        "date": date_prefix,
        "topic_idx": topic_idx,
        "sources": topic.get("sources", []),
        "article_path": str(article_path),
        "images_dir": str(output_dir / "images"),
    }
    with open(output_dir / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

    print(f"  ✅ Article saved: {article_path}")
    print(f"  Length: {len(response)} chars")

    return meta


def step_publish(topic_idx: int = 0, date_str: str = None) -> bool:
    """Step 3: Publish to WeChat draft."""
    print(f"\n{'='*60}")
    print(f"📤 Publishing to WeChat")
    print(f"{'='*60}")

    # Find article directory
    if date_str:
        prefix = date_str
    else:
        prefix = datetime.date.today().isoformat()

    candidates = sorted(WEEKLY_DIR.glob(f"{prefix}-*"), reverse=True)

    if not candidates:
        print(f"  ❌ No article directory found for prefix '{prefix}'")
        return False

    output_dir = candidates[topic_idx] if topic_idx < len(candidates) else candidates[0]
    article_path = output_dir / "article.md"
    images_dir = output_dir / "images"
    meta_path = output_dir / "meta.json"

    if not article_path.exists():
        print(f"  ❌ No article.md in {output_dir}")
        return False

    # Load digest from meta
    digest = ""
    if meta_path.exists():
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
            digest = meta.get("digest", "")

    # Validate publish script
    if not WECHAT_PUBLISH.exists():
        print(f"  ⚠️  wechat_publish.py not found at: {WECHAT_PUBLISH}")
        print(f"  Article prepared at: {article_path}")
        print(f"  Digest: {digest}")
        return False

    # Run publish
    cmd = [
        sys.executable, str(WECHAT_PUBLISH),
        "--article", str(article_path),
    ]
    if images_dir.exists():
        cmd.extend(["--images-dir", str(images_dir)])
    if digest:
        cmd.extend(["--digest", digest])

    print(f"  Running: {' '.join(str(c) for c in cmd)}")
    # Pass current env (which includes .env vars loaded at top of script) to subprocess
    env = os.environ.copy()
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)

    print(result.stdout)
    if result.stderr:
        print(f"  stderr: {result.stderr[:300]}")

    return result.returncode == 0


def step_list():
    """List all weekly drafts and outlines."""
    # Show current outlines
    if OUTLINES_FILE.exists():
        with open(OUTLINES_FILE, "r", encoding="utf-8") as f:
            outlines = json.load(f)
        print(f"\n📋 Current Outlines ({outlines.get('date', '?')})  [{outlines.get('material_count', '?')} materials]:")
        for i, t in enumerate(outlines.get("topics", [])):
            print(f"  [{i}] {t['title']}")
            print(f"      ↳ Confidence: {t.get('confidence', '?')}  |  Digest: {t.get('digest', '')}")

    # Show all weekly article directories
    print(f"\n📂 Weekly Articles ({WEEKLY_DIR}):")
    dirs = sorted(WEEKLY_DIR.glob("*"))
    if dirs:
        for d in dirs:
            if d.is_dir():
                article = d / "article.md"
                meta = d / "meta.json"
                if article.exists():
                    title = "(unknown)"
                    if meta.exists():
                        with open(meta, "r", encoding="utf-8") as f:
                            title = json.load(f).get("title", title)
                    print(f"  📄 {d.name}/ — {title}")
    else:
        print("  (empty)")

    print()


def main():
    parser = argparse.ArgumentParser(
        description="Weekly synthesis article pipeline — topic clustering → write → publish"
    )
    parser.add_argument("--step", type=str,
                        choices=["outline", "write", "publish", "list"],
                        default="list",
                        help="Pipeline step to execute")
    parser.add_argument("--topic-idx", type=int, default=0,
                        help="Topic index (for write and publish steps)")
    parser.add_argument("--date", type=str, default=None,
                        help="Reference date (YYYY-MM-DD). Uses today if omitted.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be done without generating")
    parser.add_argument("--model", type=str, default=get_model("synthesis"),
                        choices=["deepseek-v4-flash", "openai-codex/gpt-5.4"],
                        help="Writing model")
    args = parser.parse_args()

    if args.step == "outline":
        topics = step_outline(args.date, args.model)
        if topics:
            print(f"\n✅ Outline complete! {len(topics)} topics ready for review.")
            return 0
        else:
            print(f"\n❌ No topics identified.")
            return 1

    elif args.step == "write":
        meta = step_write(args.topic_idx, args.date, args.model, args.dry_run)
        if meta:
            print(f"\n✅ Article written: {meta['title']}")
            return 0
        else:
            print(f"\n❌ Article writing failed.")
            return 1

    elif args.step == "publish":
        success = step_publish(args.topic_idx, args.date)
        print(f"\n{'✅ Published to WeChat draft!' if success else '❌ Publish failed'}")
        return 0 if success else 1

    elif args.step == "list":
        step_list()
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
