#!/usr/bin/env python3
"""
Daily Case Study Decomposition Pipeline.

Agent-driven pipeline for deep-diving enterprise AI case studies.

区别于公众号文章（write_article.py）：
- 案例拆解重信息密度、多信源交叉、可复盘细节
- 公众号文章重推理链、叙事驱动、传播性
- 各自独立 Prompt，互不干扰

Pipeline steps:
  1. Discover case — pick from Stanford Playbook / daily collection / external search
  2. Write case study — LLM with CASE_STUDY_PROMPT
  3. Polish — polish_article.py
  4. Search images — image_search.py
  5. Embed images — embed_images.py
  6. Publish — wechat_publish.py (optional)

Usage:
  python3 decompose_case_study.py [--date YYYY-MM-DD] [--dry-run] [--materials /path/to/search-results.json]
  python3 decompose_case_study.py --case-index 0           # Force pick specific playbook case
  python3 decompose_case_study.py --external               # Use external source pool
  python3 decompose_case_study.py --external --search "keyword"  # Search for cases
"""

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import get_date_str
from lib.materials import get_all_collected_urls, read_material_content
from lib.llm import call_model
from lib.models import get_model
WORKSPACE_DIR = COLLECTOR_DIR.parent

# ── Config ──
CASE_WRITING_MODEL = get_model("case")
OUTPUT_DIR = WORKSPACE_DIR / "wechat-drafts" / "case-studies"
PLAYBOOK_FILE = COLLECTOR_DIR / "reports" / "EnterpriseAIPlaybook.txt"

# External source state tracking
EXTERNAL_INDEX_FILE = COLLECTOR_DIR / ".state" / "external_case_index.txt"
EXTERNAL_USED_FILE = COLLECTOR_DIR / ".state" / "external_used_urls.txt"

# ── External case source definitions ──
# Each source: { "name", "url", "type": "rss|blog|search", "language": "en", "topics": [...] }
# These are standalone case study collection sites, not general AI news.
EXTERNAL_SOURCES = [
    {
        "name": "case-studies.ai",
        "url": "https://case-studies.ai/",
        "type": "collection",
        "language": "en",
        "description": "Human-curated enterprise AI case study library. 8 categories (document intelligence, customer engagement, process automation, risk, operations, production, supply chain, engineering). Each case has brief/solution/implementation/evaluation.",
        "topics": ["enterprise-ai", "case-study", "production"],
    },
    {
        "name": "ninetwothree blog",
        "url": "https://www.ninetwothree.co/blog/",
        "type": "blog",
        "language": "en",
        "description": "AI adoption case studies with measurable business results",
        "topics": ["ai-adoption", "case-study", "enterprise"],
    },
    {
        "name": "Applied (theapplied.co)",
        "url": "https://theapplied.co/use-cases",
        "type": "collection",
        "language": "en",
        "description": "Real AI use cases with verified metrics. Organized by industry, tool, and company.",
        "topics": ["use-case", "ai-agent", "enterprise"],
    },
    {
        "name": "Intercom Blog",
        "url": "https://www.intercom.com/blog",
        "type": "blog",
        "language": "en",
        "description": "Customer service AI case studies, AI agent deployment stories",
        "topics": ["customer-service", "ai-agent", "case-study"],
    },
    {
        "name": "Retool Blog",
        "url": "https://retool.com/blog",
        "type": "blog",
        "language": "en",
        "description": "Enterprise AI deployment stories, internal tools at scale. Features detailed company case studies (Pernod Ricard, etc.)",
        "topics": ["enterprise", "internal-tools", "case-study"],
    },
    {
        "name": "LangChain Blog (customers)",
        "url": "https://blog.langchain.dev/",
        "type": "blog",
        "language": "en",
        "description": "Customer case studies of AI agent deployments using LangChain/LangGraph. C.H. Robinson, Klarna, etc.",
        "topics": ["ai-agent", "case-study", "llm"],
    },
    {
        "name": "AWS ML Blog",
        "url": "https://aws.amazon.com/blogs/machine-learning/",
        "type": "blog",
        "language": "en",
        "description": "Production AI deployment case studies on AWS infrastructure",
        "topics": ["mlops", "deployment", "engineering"],
    },
    {
        "name": "Google Cloud Blog (AI)",
        "url": "https://cloud.google.com/blog/products/ai-machine-learning",
        "type": "blog",
        "language": "en",
        "description": "Enterprise AI/ML deployment case studies on Google Cloud",
        "topics": ["mlops", "deployment", "cloud"],
    },
    {
        "name": "Moveworks Customer Stories",
        "url": "https://www.moveworks.com/us/en/customers",
        "type": "blog",
        "language": "en",
        "description": "Enterprise AI agent customer stories with quantified results",
        "topics": ["ai-agent", "enterprise", "case-study"],
    },
    {
        "name": "Converteo Blog",
        "url": "https://converteo.com/en/blog/",
        "type": "blog",
        "language": "en",
        "description": "AI agent deployment case studies (Lacoste 4-month agent rollout, etc.)",
        "topics": ["ai-agent", "retail", "case-study"],
    },
]

# ── Search queries for AnySearch when --external --search is used ──
EXTERNAL_SEARCH_QUERIES = [
    "enterprise AI agent production deployment results automation rate ROI case study",
    "AI implementation case study company results metrics deployment 2025",
    "how company implemented AI agent customer service automation results",
    "enterprise AI copilot deployment production metrics lessons learned",
    "AI transforming business process case study quantified results",
    "company built AI assistant replaced manual process efficiency gains",
    "enterprise AI adoption real world story with numbers and timeline",
]


# ════════════════════════════════════════════════════════════════
#  CASE_STUDY_PROMPT — 独立 Prompt，不走 WRITING_SYSTEM_PROMPT
# ════════════════════════════════════════════════════════════════

CASE_STUDY_PROMPT = """你是一个企业AI落地分析师。你的任务是深度拆解一个企业AI落地案例，写成一篇高信息密度的案例解读。

## 文章定位

这类文章不是公众号文章，不追求转发和传播。它是给企业决策者看的案例参考文档，定位是：
- **快读**：开头150字就能知道案例的全貌和价值
- **深读**：想要细节的人能找到具体数字、决策逻辑、实施过程
- **带走**：读完能直接回答"我能不能参考这个？我第一件事该做什么？"

## 硬性规则（必须严格遵守）

1. **正文配图**：在合适位置（每500-800字）插入配图标记 `![](./images/image-NNN-keyword.jpg)`（NNN为三位数字序号，keyword为英文关键词）。配图应与内容相关——架构图、流程图、数据图、照片等。一篇文章**至少插入3张配图标记**。注意：仅仅插入标记即可，图片文件由后续步骤自动搜索嵌入。示例：`![](./images/image-001-kyc-agent-workflow.jpg)`
2. **排版要求**：关键数据用**加粗**，对比用列表，引用用 `>` blockquote。不要整段都是文字，每300字至少有一个排版变化（加粗/列表/引用/分隔线）。
3. 不要使用「这意味着」「这告诉我们」「这说明」等总结型句子。
4. 不要使用「不是……而是……」话术结构。

每一篇必须包含以下完整信息，缺一不可（素材不够时从备选区域获取补充）：

## 每篇案例必须涵盖的信息点

如果你收到的素材缺少以下任何信息，**必须主动在文中标注"（待补充）"**，由编辑判断是否需要搜索补充。

### 1. 公司/组织背景
- 公司名称（如果素材匿名，标注"（匿名化）"）
- 行业
- 规模（收入、员工数、年处理量等量化数据）
- 核心业务

### 2. 问题定义
- 要解决的问题具体是什么？（不是"效率低"，而是"每年10万张发票要7个人处理"）
- 这个问题的量级（多少人、多少时间、多少成本）
- 为什么之前没人解决？（技术原因？组织原因？优先级原因？）

### 3. 解决方案——AI到底做了什么（本节必须最详细）
- **AI 的工作流程**：AI 系统接收到什么输入？经过什么模型处理？输出什么？这些输出如何影响真实业务流程？用「输入→处理→输出→影响」的结构写清楚
- **AI 的决策边界**：AI 拥有哪些决策权？哪些事 AI 可以自己决定？哪些事必须经过人？决策的权限边界在哪里？
- **人和AI的分工**：哪些步骤是人做的？哪些步骤是AI做的？人机交互界面是什么样的？人是怎么复核/干预/兜底的？
- **技术方案**（具体产品/模型/架构）
- **为什么选这套方案？**（成本、兼容性、团队能力？）
- **实施过程**分几个阶段？每阶段做了什么？花了多久？

### 4. 关键细节——那些看起来和AI无关但决定成败的动作
- 流程简化做了吗？谁做的？多久？
- 数据清洗/标注谁做的？业务专家参与了吗？
- 高层支持力度如何？
- 内部知识转移怎么做的？
- 遇到什么阻力？怎么解决的？

### 5. 结果
- 量化结果：人力变化、准确率、处理时间、成本节约
- 组织变化：团队角色变化、被释放的人去哪了、人员是否被裁
- 时间线：从启动到上线多久

### 6. 决策逻辑——那些反直觉但关键的判断
- 为什么接受某个准确率阈值？
- 为什么选某个技术方案而不是更主流的？
- 为什么安排某个角色参与？
- AI做错的案例：AI有没有犯过明显的错？怎么被发现的？怎么修正的？这比「AI做对了什么」对读者更有价值

### 7. 可复用判断（2-3条）
每条 = 一句话结论 + 一两句话解释 + **适用条件**（什么场景能用，什么场景不能用）

## 合并且去重的文章结构

按以下顺序组织，但所有信源信息打碎重组，**禁止按来源切分章节**：

### 开头段（100-150字）
一段话讲清楚：什么公司？什么行业？什么问题？什么方案？什么结果？不许铺垫。

### 背景与问题
公司背景 → 问题定义（带上数据）→ 为什么之前没人解决

### 方案与执行
技术方案 → **AI的工作流程和决策边界（本节最详细部分）** → 人机分工 → 执行阶段拆分 → 关键细节（尤其那些和AI无关的决定性动作）

### 结果与决策
量化结果 → 组织变化 → 容易忽略的关键判断

### 可复用判断（2-3条，含适用条件）
（无独立"边界说明"节——适用条件直接放在每条判断里）

## 中文写作规范

- 全文使用中文标点符号：句号用。，逗号用，，引号用「」和『』（嵌套时外「内『』），括号用（），书名号用《》，破折号用——，省略号用……
- 禁止使用英文双引号""和英文单引号''
- 英文术语、缩写保留原文不翻译，但不需要加引号

## 写作风格固定模板

1. **句式**：短句为主。长句不超过40字。一段不超过5句。
2. **开头禁用语**：禁止「近日」「随着AI技术的」「在这个充满变革的」「我们注意到」
3. **来源处理**：
   - 数据类信息用「行业研究显示」「多个案例表明」「同类项目数据显示」等统一表述
   - 绝对禁止出现具体来源名称（Stanford、Ampcome、923、XX报告、XX商学院、XX咨询）
4. **不可用词汇**：「赋能」「抓手」「闭环」「沉淀」「打法」「底层逻辑」「重构」「生态」
5. **全文长度**：1500-2000字，不超过2000字

## 输出格式

```markdown
# 标题：公司/行业 + 核心判断

（正文，纯 Markdown，包含配图标记）
```

摘要放在文章第一行单独输出：
摘要: 一句话概括案例核心（不超过80字）

注意：引用行业数据/同类案例时，**禁止出现来源名称**。
"""


# ════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════

def extract_digest(text: str) -> str:
    """Extract '摘要:' line from the model response."""
    m = re.search(r"^摘要[：:]\s*(.+)", text, re.MULTILINE)
    result = m.group(1).strip() if m else ""
    if not result:
        # Fallback: use first 100 chars
        cleaned = re.sub(r'^#.*\n', '', text, count=1).strip()
        result = cleaned[:100] + "…" if len(cleaned) > 100 else cleaned
    return result


def strip_digest(text: str) -> str:
    """Remove the digest line from article text."""
    return re.sub(r"^摘要[：:].*\n?", "", text, flags=re.MULTILINE).strip()


def fetch_url_content(url: str, timeout: int = 15) -> Optional[str]:
    """Fetch URL content using web_fetch (via subprocess). Falls back to requests."""
    import subprocess
    try:
        # Try python requests first
        import requests
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; ContentBot/1.0)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        resp = requests.get(url, headers=headers, timeout=timeout)
        if resp.status_code == 200:
            content = resp.text
            # Simple text extraction: strip HTML tags
            import html
            text = re.sub(r'<[^>]+>', ' ', content)
            text = re.sub(r'\s+', ' ', text).strip()
            return text[:15000]
    except Exception:
        pass
    return None


def search_with_anysearch(query: str, max_results: int = 10) -> list[dict]:
    """Search using AnySearch API."""
    api_key = os.environ.get("ANYSEARCH_API_KEY", "")
    if not api_key:
        print("⚠️ ANYSEARCH_API_KEY 缺失，案例检索将失败（请在 .env / 环境变量中配置）")
    import subprocess, json
    cmd = [
        "curl", "-s", "-X", "POST", "https://api.anysearch.com/v1/search",
        "-H", f"Authorization: Bearer {api_key}",
        "-H", "Content-Type: application/json",
        "-d", json.dumps({
            "query": query,
            "domains": ["business", "tech"],
            "content_types": ["web", "news"],
            "max_results": max_results,
        })
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        data = json.loads(result.stdout)
        items = data.get("data", {}).get("results", [])
        return items
    except Exception as e:
        print(f"  ⚠️ AnySearch error: {e}")
        return []


# ════════════════════════════════════════════════════════════════
#  Step 1: Discover Case — External Sources
# ════════════════════════════════════════════════════════════════

def discover_case_external(search_query: Optional[str] = None) -> Optional[dict]:
    """
    Discover a case study from external sources.
    
    Strategy (tried in order):
    1. Search AnySearch for high-quality case studies (if search_query provided)
    2. Scan known collection sites (case-studies.ai, theapplied.co)
    3. Fall back to pre-configured search queries
    
    Returns: dict with url, content, title, source, or None
    """
    # Load used URLs to avoid repeats
    used_urls = _load_used_external_urls()
    
    # Strategy 1: AnySearch with specific query
    if search_query:
        print(f"  🔍 Searching for: {search_query}")
        results = search_with_anysearch(search_query)
        for r in results:
            url = r.get("url", "").strip()
            if not url or url in used_urls:
                continue
            content_snippet = r.get("content", "") or r.get("snippet", "") or ""
            if len(content_snippet) < 200:
                continue
            title = r.get("title", "") or ""
            print(f"  ✓ Found: {title[:80]}")
            return {
                "url": url,
                "title": title,
                "content": f"Title: {title}\n\n{content_snippet}",
                "source": "anysearch",
                "search_query": search_query,
            }
    
    # Strategy 2: Skip collection site root pages — they don't contain a single case
    # and cause repeat issues. Rely on AnySearch queries for specific case URLs.
    
    # Strategy 3: Try pre-configured search queries
    for q in EXTERNAL_SEARCH_QUERIES:
        print(f"  🔍 Searching: {q[:60]}...")
        results = search_with_anysearch(q, max_results=5)
        for r in results:
            url = r.get("url", "").strip()
            if not url or url in used_urls:
                continue
            content = r.get("content", "") or r.get("snippet", "") or ""
            if len(content) < 300:
                continue
            title = r.get("title", "") or ""
            return {
                "url": url,
                "title": title,
                "content": content[:10000],
                "source": "anysearch",
                "search_query": q,
            }
    
    return None


def _is_generic_domain(url: str) -> bool:
    """Check if URL is just a domain (no specific path to a case)."""
    generic_domains = [
        "case-studies.ai", "theapplied.co", "ninetwothree.co",
        "ninetwothree.com", "intercom.com", "retool.com",
        "langchain.dev", "blog.langchain.dev", "moveworks.com",
        "aws.amazon.com", "cloud.google.com",
    ]
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        path = parsed.path.rstrip("/")
        if not path or path == "":
            return any(g in domain for g in generic_domains)
    except Exception:
        pass
    return False


def _has_url_been_used_as_source(url: str) -> bool:
    """Check if a URL has been used in any existing case study's materials.json."""
    output_dir = OUTPUT_DIR
    if not output_dir.exists():
        return False
    import json
    for d in sorted(output_dir.iterdir()):
        if not d.is_dir():
            continue
        mat_path = d / "materials.json"
        if mat_path.exists():
            try:
                mats = json.loads(mat_path.read_text(encoding="utf-8"))
                if isinstance(mats, list):
                    for m in mats:
                        u = m.get("url", "").rstrip("/")
                        if u == url.rstrip("/"):
                            return True
                elif isinstance(mats, dict):
                    u = mats.get("url", "").rstrip("/")
                    if u == url.rstrip("/"):
                        return True
            except Exception:
                continue
    return False


def _load_used_external_urls() -> set:
    """Load set of previously used external case URLs."""
    used = set()
    if EXTERNAL_USED_FILE.exists():
        for line in EXTERNAL_USED_FILE.read_text().strip().split("\n"):
            url = line.strip()
            # Skip generic domain entries — only track specific case URLs
            if url and not _is_generic_domain(url):
                used.add(url)
    return used


def _publish_to_wechat_draft(out_dir: str, date_str: str) -> bool:
    """Publish case study article as a WeChat draft."""
    import requests
    out_path = Path(out_dir)
    md_path = out_path / "article.md"
    img_dir = out_path / "images"
    if not md_path.exists():
        print(f"  ❌ Article not found for publishing: {md_path}")
        return False

    md_text = md_path.read_text(encoding="utf-8")

    # Load meta.json for title/digest
    meta_path = out_path / "meta.json"
    meta_data = {}
    if meta_path.exists():
        try:
            meta_data = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Title: prefer meta.json title, then first non-H1 line of body
    title = meta_data.get("title", "")
    if not title:
        first_line = md_text.split("\n")[0].strip().lstrip("# ").strip()
        title = first_line[:60] if first_line else f"案例拆解 {date_str}"

    # Digest: prefer meta.json, then extract_digest, then first paragraph
    digest = meta_data.get("digest", "")
    if not digest:
        digest = extract_digest(md_text)
    if not digest:
        first_para = md_text.split("\n\n")[1] if len(md_text.split("\n\n")) > 1 else ""
        digest = first_para.strip().replace("\n", " ")[:60]

    # Import WeChat publisher with purple theme
    sys.path.insert(0, str(Path(__file__).parent))
    from wechat_publish import WeChatPublisher

    # Load env
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        with open(env_path) as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and "=" in _line and not _line.startswith("#"):
                    _k, _v = _line.split("=", 1)
                    _v = _v.strip().strip("\"'").strip()
                    if _v and not os.environ.get(_k.strip()):
                        os.environ[_k.strip()] = _v

    app_id = os.environ.get("WECHAT_APP_ID", "")
    app_secret = os.environ.get("WECHAT_APP_SECRET", "")
    if not app_id or not app_secret:
        print("  ❌ WeChat credentials not found")
        return False

    print(f"\n📤 Publishing case study to WeChat draft...")
    publisher = WeChatPublisher(app_id, app_secret)

    # Upload article images and replace in markdown with WeChat CDN URLs
    article_md = md_text
    uploads = publisher.upload_article_images_as_urls(str(img_dir))

    def _replace_img(m):
        basename = os.path.basename(m.group(2))
        url = uploads.get(basename)
        if url:
            return f'![{m.group(1)}]({url})'
        return m.group(0)
    article_md = re.sub(r'!\[([^\]]*)\]\(([^)]+)\)', _replace_img, article_md)

    # Strip backticks wrapping ![]() image lines (prevent them from being <code>)
    article_md = re.sub(r'^`(!\[[^\]]*\]\([^)]+\))`\s*$', r'\1', article_md, flags=re.MULTILINE)

    # Convert with purple theme (handles tables, headings, lists)
    html_content = publisher._markdown_to_wechat_html(article_md)

    # Truncate title to WeChat limits (keep under 85 bytes for CJK titles)
    while len(title.encode('utf-8')) > 85:
        title = title[:-1]

    # Upload cover as thumb
    cover_path = None
    for candidate in ["cover-1x1.jpg", "cover-wide.jpg"]:
        p = img_dir / candidate
        if p.exists():
            cover_path = p
            break
    if not cover_path:
        image_exts = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'}
        imgs = [p for p in img_dir.iterdir() if p.suffix.lower() in image_exts]
        cover_path = imgs[0] if imgs else None

    thumb_media_id = ""
    if cover_path:
        try:
            from PIL import Image as PILImage
            thumb_path = img_dir / "cover_thumb.jpg"
            im = PILImage.open(str(cover_path)).convert('RGB')
            sz = min(im.size)
            im = im.crop(((im.width - sz)//2, (im.height - sz)//2, (im.width + sz)//2, (im.height + sz)//2))
            im = im.resize((300, 300), PILImage.LANCZOS)
            im.save(thumb_path, 'JPEG', quality=85)
            thumb_media_id = publisher.upload_image(str(thumb_path))
            if thumb_media_id:
                print(f"  Cover thumb uploaded: {thumb_media_id[:20]}...")
            else:
                print(f"  ⚠️ Cover upload returned empty media_id")
        except Exception as e:
            print(f"  ⚠️ Cover thumb upload failed ({e})")
    else:
        print("  ⚠️ No cover image found")

    # Create draft via publisher (handles encoding correctly)
    try:
        media_id = publisher.create_draft({
            'title': title,
            'digest': digest,
            'content': html_content,
        }, thumb_media_id)
        print(f'  ✅ Draft created: {media_id}')
        return True
    except Exception as e:
        print(f'  ❌ Draft creation failed: {e}')
        return False


def _mark_external_url_used(url: str):
    """Mark a URL as used to avoid repeats.
    Skips generic domain entries (e.g. "case-studies.ai" without a specific path)."""
    if _is_generic_domain(url):
        print(f"  ⏭️ Skipping generic domain: {url}")
        return
    EXTERNAL_USED_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(EXTERNAL_USED_FILE, "a") as f:
        f.write(url.strip().rstrip("/") + "\n")


# ════════════════════════════════════════════════════════════════
#  Step 1 (continued): Discover Case — Collection / Playbook
# ════════════════════════════════════════════════════════════════

def discover_case_from_collection(date_str: str) -> Optional[dict]:
    """Check daily collection for case-study tagged items."""
    all_items = get_all_collected_urls()
    if not all_items:
        return None
    case_keywords = [
        "case study", "use case", "deployment", "production",
        "implemented", "real-world", "scaling", "enterprise",
    ]
    case_matches = []
    for item in all_items:
        url = item.get("url", "").lower()
        line = item.get("line", "").lower()
        combined = url + " " + line
        match_count = sum(1 for kw in case_keywords if kw in combined)
        if match_count >= 2:
            case_matches.append(item)
    return case_matches[0] if case_matches else None


def discover_case_from_playbook(try_index: Optional[int] = None) -> Optional[dict]:
    """Discover a case from Stanford Playbook text file."""
    if not PLAYBOOK_FILE.exists():
        return None

    # Case chapter markers in the Playbook
    case_headers = [
        "Invoice Processing at a Logistics Company",
        "Recruiting at a Translation Services Company",
        "Marketing Content at a Financial Services Company",
        "Field Service at a Semiconductor Company",
        "Security Operations at a Technology Services",
        "Engineering at an Education Technology Company",
        "Customer Relations at a Call Center",
        "Procurement at a Construction Services Company",
        "Customer-Facing at a Large Retail Bank",
        "Customer Support at a Technology Company",
    ]

    # State file for round-robin
    state_file = COLLECTOR_DIR / ".state" / "playbook_case_index.txt"
    state_file.parent.mkdir(parents=True, exist_ok=True)

    if try_index is not None:
        next_idx = try_index % len(case_headers)
    else:
        if state_file.exists():
            try:
                last_idx = int(state_file.read_text().strip())
            except ValueError:
                last_idx = -1
        else:
            last_idx = -1
        next_idx = (last_idx + 1) % len(case_headers)

    # Round counter: track how many times we've cycled through
    round_file = COLLECTOR_DIR / ".state" / "playbook_round.txt"
    round_file.parent.mkdir(parents=True, exist_ok=True)
    
    current_round = 0
    if round_file.exists():
        try:
            current_round = int(round_file.read_text().strip())
        except ValueError:
            current_round = 0
    
    # Check if this is a new full cycle
    if next_idx == 0:
        current_round += 1
        round_file.write_text(str(current_round))
        state_file.write_text("0")
        if current_round > 1:
            # We've exhausted all cases and completed at least one full round
            # Signal to switch to external sources
            print("  ✓ Playbook cases exhausted (round 2+). Switching to external sources.")
            return None  # Will trigger external discovery
    
    state_file.write_text(str(next_idx))

    selected_header = case_headers[next_idx]
    text = PLAYBOOK_FILE.read_text(encoding="utf-8")

    # Extract case section: find the header -> scan for next case header or "Chapter"
    start = text.find(selected_header)
    if start < 0:
        return None

    # Find end: next case header, next Chapter, or PDF page marker
    candidates = []
    for h in case_headers:
        if h == selected_header:
            continue
        pos = text.find(h, start + len(selected_header))
        if pos > 0:
            candidates.append(pos)
    # Also check for Chapter markers
    for ch in range(2, 12):
        m = re.search(rf"(?m)^Chapter {ch}\b", text[start + len(selected_header):])
        if m:
            candidates.append(start + len(selected_header) + m.start())

    if candidates:
        end = min(candidates)
    else:
        end = start + 5000  # safety cap

    content = text[start:end].strip()

    return {
        "url": f"playbook://case#{next_idx}/{selected_header.lower().replace(' ', '-')}",
        "content": content[:10000],
        "source": "stanford-playbook",
        "title": selected_header,
    }


# ════════════════════════════════════════════════════════════════
#  Step 2: Build context + Write
# ════════════════════════════════════════════════════════════════

def build_case_context(materials: list[dict]) -> str:
    """Build a context string from case + supplementary materials."""
    parts = []
    for i, m in enumerate(materials, 1):
        content = m.get("content", "")
        url = m.get("url", "")
        title = m.get("title", "")
        label = title or url
        parts.append(f"=== 素材 {i}: {label} ===\n{content[:8000]}")
    return "\n\n".join(parts)


def write_case(
    date_str: str,
    materials: list[dict],
    dry_run: bool = False,
) -> Optional[str]:
    """
    Write a case study using CASE_STUDY_PROMPT.
    Returns output directory path on success, None on failure.
    """
    print(f"\n{'='*60}")
    print(f"📋 Case Study Writing — {date_str}")
    print(f"{'='*60}")

    context = build_case_context(materials)
    user_prompt = f"""请根据以下素材，写一篇企业AI案例拆解。

素材内容：
{context}

请严格按照上述要求输出完整 Markdown 文章。摘要行放在第一行。正文不要以 # 标题开头，直接从开头段开始写。"""

    messages = [
        {"role": "system", "content": CASE_STUDY_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    print(f"\n5️⃣  Calling LLM for case writing ({CASE_WRITING_MODEL})...")
    response = call_model(messages, temperature=0.7, max_tokens=8192, model=CASE_WRITING_MODEL)
    if not response:
        print("  ❌ Model returned no response")
        return None

    # Extract digest
    digest = extract_digest(response)
    article_text = strip_digest(response)

    # Extract title BEFORE stripping so we still get a good directory name
    title_match = re.search(r'^#\s+(.+)$', article_text, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else (digest or "case-study")

    # Strip the H1 title line — WeChat draft already shows the title separately,
    # so having # Title in the body causes title duplication in published articles.
    article_text = re.sub(r'^#\s+.+\n?', '', article_text, count=1).lstrip()

    if len(article_text) < 300:
        print(f"  ❌ Article too short ({len(article_text)} chars), likely failed")
        return None

    print(f"  ✓ Article generated: {len(article_text)} chars")

    # Determine output directory (use extracted title, fallback to digest)
    title_slug = re.sub(r'[^\w\u4e00-\u9fff-]', '-', title)[:60].strip('-').lower()
    output_dir = OUTPUT_DIR / f"{date_str}-{title_slug}"
    images_dir = output_dir / "images"

    if dry_run:
        print(f"\n  [Dry run] Would write to: {output_dir}")
        print(f"\n  Article preview:\n{article_text[:800]}...")
        return str(output_dir)

    output_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)

    # Save article
    article_path = output_dir / "article.md"
    article_path.write_text(article_text, encoding="utf-8")
    print(f"  ✓ Article saved: {article_path}")

    # Save title + digest to meta.json
    if digest:
        meta = {"title": title, "digest": digest, "date": date_str, "materials_count": len(materials)}
        meta["materials"] = [{"url": m.get("url", ""), "title": m.get("title", "")} for m in materials]
        meta_path = output_dir / "meta.json"
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  ✓ Digest: {digest}")
    else:
        print(f"  ⚠️ No digest extracted")

    # Save supplementary materials for reference
    materials_path = output_dir / "materials.json"
    materials_path.write_text(json.dumps(materials, ensure_ascii=False, indent=2), encoding="utf-8")

    return str(output_dir)


def _generate_image_queries(article_text: str, count: int) -> dict[int, str]:
    """Generate content-aware search queries for each image placeholder.

    Extracts key terms near each image placeholder in the article
    to produce better Pexels search queries.
    """
    queries: dict[int, str] = {}
    # Find image markers with surrounding context
    pattern = re.compile(r'!\[([^\]]*)\]\(\./images/image-\d+-.*?\)')
    matches = list(pattern.finditer(article_text))

    for m in matches:
        alt = m.group(1)
        # Extract number from marker
        num_match = re.search(r'image-(\d+)', m.group(0))
        if not num_match:
            continue
        idx = int(num_match.group(1))

        # Use alt text as primary query
        if alt and alt != "配图" and not alt.startswith("配图"):
            queries[idx] = f"enterprise AI {alt}"
        else:
            # Fallback: grab ~100 chars before the marker
            start = max(0, m.start() - 100)
            context = article_text[start:m.start()]
            # Extract key nouns (Chinese 2-4 char words or English keywords)
            key_terms = re.findall(r'[\u4e00-\u9fff]{2,6}', context)
            # Pick last 2-3 non-stop meaningful terms
            stop_words = {"这个", "那个", "什么", "怎么", "可以", "没有", "一个", "一些",
                         "这些", "那些", "一下", "我们", "他们", "你们"}
            keywords = [t for t in key_terms if t not in stop_words][-3:]
            query = "enterprise " + " ".join(keywords[-2:]) if keywords else "enterprise AI case study"
            queries[idx] = query

    # Fill any gaps
    for i in range(1, count + 1):
        if i not in queries:
            queries[i] = "enterprise AI case study"

    return queries


# ════════════════════════════════════════════════════════════════
#  Step 4-6: Polish + Image + Publish
# ════════════════════════════════════════════════════════════════

def post_process(output_dir: str) -> bool:
    """Run polish, image search, embed on the written case study."""
    out_path = Path(output_dir)
    article_path = out_path / "article.md"

    if not article_path.exists():
        print(f"  ❌ Article not found: {article_path}")
        return False

    article_text = article_path.read_text(encoding="utf-8")

    # Count image placeholders
    image_count = len(re.findall(r'!\[.*?\]\(.*?\)', article_text))
    if image_count == 0:
        print("  ⚠️ No placeholders found, auto-injecting 3 image placeholders with context-aware queries...")
        paragraphs = article_text.split("\n\n")
        new_article = []
        char_count = 0
        img_idx = 1
        for para in paragraphs:
            new_article.append(para)
            char_count += len(para)
            if char_count > 500 and img_idx <= 3:
                # Extract meaningful keywords from surrounding context for better Pexels search
                context_words = re.findall(r'[\u4e00-\u9fff]{2,6}', para[:150])
                stop_words = {"这个", "那个", "什么", "怎么", "可以", "没有", "一个", "一些",
                             "这些", "那些", "一下", "我们", "他们", "你们", "问题", "就是",
                             "不是", "还是", "已经", "可能", "所以", "因为", "如果",
                             "但是", "而且"}
                keywords = [w for w in context_words if w not in stop_words][-3:]
                desc = keywords[-1] if keywords else f"示意图{img_idx}"
                new_article.append(f"\n![{desc}](./images/image-{img_idx:03d}-placeholder.jpeg)\n")
                img_idx += 1
                char_count = 0
        article_text = "\n\n".join(new_article)
        article_path.write_text(article_text, encoding="utf-8")
        image_count = img_idx - 1
        print(f"  ✓ Injected {image_count} context-aware placeholders into article")

    if image_count == 0:
        print("  ⚠️ No images to search")
        return False

    # Generate queries
    queries = _generate_image_queries(article_text, image_count)

    # Download images
    print(f"\n🖼️  Downloading {len(queries)} images...")
    images_dir = out_path / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    # Use image_search script
    image_search_py = COLLECTOR_DIR / "image_search.py"
    embed_images_py = COLLECTOR_DIR / "embed_images.py"

    if not image_search_py.exists():
        print(f"  ⚠️ image_search.py not found, skipping images")
        return True

    success = True
    for idx in sorted(queries.keys()):
        query = queries[idx]
        print(f"\n  [{idx}/{image_count}] Searching: {query}")
        try:
            result = subprocess.run(
                [sys.executable, str(image_search_py), query,
                 str(images_dir), "--index", str(idx)],
                capture_output=True, text=True, timeout=30,
                env={**os.environ, "PEXELS_API_KEY": os.environ.get("PEXELS_API_KEY", "")}
            )
            if result.returncode == 0:
                print(f"    ✓ Image {idx} downloaded")
            else:
                print(f"    ⚠️ Image {idx} search failed: {result.stderr[:100]}")
                success = False
        except Exception as e:
            print(f"    ⚠️ Image {idx} error: {e}")
            success = False

    # If all image searches failed, generate placeholder gradient images
    # so the article isn't entirely text-only
    images_dir_path = Path(images_dir)
    existing_images = list(images_dir_path.glob("image-*"))
    if not existing_images and queries:
        print(f"  ⚠️ No images downloaded from search. Generating placeholder images...")
        try:
            from PIL import Image, ImageDraw, ImageFont
            for idx in sorted(queries.keys()):
                query_text = queries[idx][:30]
                fname = f"image-{idx:03d}-placeholder.jpeg"
                fpath = images_dir_path / fname
                img = Image.new('RGB', (800, 450), color=(35, 88, 160))
                draw = ImageDraw.Draw(img)
                # Try to find a font
                font = None
                for fp in ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                           "/usr/share/fonts/TTF/DejaVuSans.ttf",
                           "/System/Library/Fonts/Helvetica.ttc"]:
                    if os.path.exists(fp):
                        font = ImageFont.truetype(fp, 20)
                        break
                if font:
                    bbox = draw.textbbox((0, 0), query_text, font=font)
                    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
                    draw.text(((800 - tw) // 2, (450 - th) // 2), query_text,
                              fill=(200, 220, 255), font=font)
                img.save(fpath, "JPEG", quality=70)
                print(f"    ✓ Generated placeholder: {fname}")
        except ImportError:
            print(f"    ⚠️ PIL not available, skipping placeholder generation")
        except Exception as e:
            print(f"    ⚠️ Placeholder generation error: {e}")

    # Embed images into article
    if embed_images_py.exists():
        print(f"\n🔗 Embedding images into article...")
        try:
            subprocess.run(
                [sys.executable, str(embed_images_py),
                 str(article_path), str(images_dir)],
                capture_output=True, text=True, timeout=30,
            )
            print(f"    ✓ Images embedded")
        except Exception as e:
            print(f"    ⚠️ Embedding error: {e}")
            success = False

    return success


# ════════════════════════════════════════════════════════════════
#  main()
# ════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Daily case study decomposition pipeline"
    )
    parser.add_argument("--date", type=str, default=None,
                        help="Date string (YYYY-MM-DD), defaults to today")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be done without writing")
    parser.add_argument("--case-index", type=int, default=None,
                        help="Pick specific playbook case index (0-based)")
    parser.add_argument("--external", action="store_true",
                        help="Use external source pool instead of Playbook")
    parser.add_argument("--search", type=str, default=None,
                        help="Search query for external case discovery")
    parser.add_argument("--materials", type=str, default=None,
                        help="Path to pre-searched materials JSON. Skips Step A-B.")
    args = parser.parse_args()

    date_str = args.date or get_date_str()
    print(f"📋 Case Study Decomposition — {date_str}")
    print("=" * 60)

    # ── Step A: Discover + Step B: Load materials ──
    if args.materials:
        # Agent already searched; just load the materials
        print("\n📦 Loading pre-searched materials...")
        mp = Path(args.materials)
        if mp.exists():
            materials = json.loads(mp.read_text(encoding="utf-8"))
            print(f"  Loaded {len(materials)} materials from {mp}")
        else:
            print(f"  ❌ Materials file not found: {mp}")
            return 1
    else:
        print("\n🔍 Step A: Discovering case...")
        case_item = None
        case_source = "unknown"

        if args.external or args.search:
            # External source mode
            print("  Using external source pool...")
            case_item = discover_case_external(args.search)
            case_source = "external"
        else:
            # Standard pipeline: try collection first
            case_item = discover_case_from_collection(date_str)
            case_source = "daily-collection"

            if not case_item:
                print("  No case-study items in collection. Trying Stanford Playbook...")
                case_item = discover_case_from_playbook(args.case_index)
                case_source = "stanford-playbook"

        if not case_item:
            print("  ❌ No case found to decompose.")
            return 1

        title = case_item.get("title", case_item.get("url", "unknown"))[:80]
        print(f"  ✓ Selected: {title}")
        print(f"    Content: {len(case_item.get('content', ''))} chars")
        print(f"    Source: {case_source}")

        # Mark external URLs as used so they don't repeat
        if case_source == "external":
            _mark_external_url_used(case_item.get("url", ""))

        print("\n🔍 Step B: Searching supplementary sources...")
        print("  ⏳ This step is handled by the agent environment.")
        print("  (Run with --materials to use pre-searched results.)")

        materials = [case_item]

    if not materials:
        print("  ❌ No materials to write")
        return 1

    # ── Step 3: Write ──
    out_dir = write_case(date_str, materials, dry_run=args.dry_run)
    if not out_dir:
        print("  ❌ Case writing failed")
        return 1

    # ── Steps 4-6: Post-process + Publish ──
    if not args.dry_run:
        post_process(out_dir)
        _publish_to_wechat_draft(out_dir, date_str)

    print(f"\n{'=' * 60}")
    print("✅ Case study complete!")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
