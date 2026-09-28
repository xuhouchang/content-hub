#!/usr/bin/env python3
"""
Full pipeline: read collected materials → LLM writing → image matching → output.

Zero agent dependency. All logic in Python.

Flow:
  1. Read all_urls.tsv for "collected" items
  2. Read corresponding Markdown files
  3. Deduplicate and filter (light rule-based, heavy LLM-based)
  4. Call DeepSeek Chat API for: 选题判断 → 推理链 → 正文写作
  5. Call image_search.py for image matching
  6. Call embed_images.py for placeholder replacement
  7. Output to wechat-articles/YYYY-MM-DD-title/

Usage:
  python3 write_article.py [--date YYYY-MM-DD] [--dry-run]
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

# ── Import LLM and materials from lib/ ──
from lib.llm import call_model
from lib import pexels_images as px
from lib.models import get_model

# ── Polish script reference ──（移到 COLLECTOR_DIR 定义之后）
from lib.materials import (
    get_all_collected_urls,
    read_material_content,
    sample_materials,
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

# ── Publish queue (decoupled async publish stage) ──
# Writing and publishing are separate scheduled stages. A successful write
# only enqueues the article; publish_worker.py is started independently by
# cron and creates the WeChat draft. See publish_worker.py.
QUEUE_DIR = COLLECTOR_DIR / "queue"

# ── System prompt for article writing ──

WRITING_SYSTEM_PROMPT = """你是一位面向「企业AI落地决策者」的公众号撰稿人。你的读者是老板、高管、业务负责人、CIO/CTO——他们只关心一件事：**AI 怎么在自己的企业里落下去**。该不该投、先投哪个环节、业务流程怎么变、组织/绩效/人才怎么配套、ROI 怎么度量、有什么代价和坑。

## 绝对禁区

严禁讨论中国的时事政治、政策、政府、监管或审查等话题。就当没看到。

你的文章不能是政治评论。

读者看完后至少会说一句"有意思"，而不是"就这？"。

## 选题主轴：企业AI落地（唯一）

本号只写「企业AI怎么落地」这一个主题。具体包括：
- 落地路径：先投哪、怎么起步、怎么规模化、什么环节该自动化、什么环节必须留人
- 落地配套：业务流程重构、组织结构变化、绩效与激励制度调整、人才与培训
- 落地度量：ROI怎么算、提效怎么量化、过程数据为什么重要
- 落地经验：真实案例、踩坑、失败原因、可复现的方法
- 落地决策：该不该投、投多少、怎么选型、怎么管风险

**不写**：AI行业的技术/产业趋势（合成数据、模型能力、算力、评测基准、大模型研究）——除非素材明确落到"企业怎么用/怎么部署/业务怎么变"，只取其中对落地有含义的部分。素材若是纯技术趋势，不要选它写。

## 写作方向：升维萃取，不停留在素材表面

**素材是证据，不是主角。** 你要从素材中提炼出对同类企业普遍成立的**落地机制与判断**，让决策者读完能带走一个落地决策依据。

- 素材是一个 case：提炼它背后的普适落地机制（"这个 case 揭示了什么对同类企业成立的规律？"），case 的细节作为证据支撑判断
- 素材是一篇报告：提取对落地有含义的结论和传导链条，讲清"这意味着管理者该做什么/该防什么"
- 素材是一个趋势判断：只保留其中与企业落地相关的部分，把传导链条说明白，落到"决策者该据此调整什么"
- 素材是某个人的观点：把他的核心论证展开，指出其边界与适用条件

每篇文章必须有一个**可迁移的落地判断**——脱离具体案例、对同类企业成立的一句话结论（例如"AI项目失败的主因是组织而非技术""推理速度是客服AI可用性的生死线""数据管道比模型知识更重要""业务流程不重构，提效只会局部化"）。具体 case 服务于这个判断，而不是反过来。

不必每篇追求"颠覆认知"。有些好文章就是"把这个事情说明白了"——但"说明白"的标准是让决策者看懂落地机制，不是复述细节。

排除项：模型/产品发布动态、技术教程、工具评测、投融资新闻、纯AI技术/产业趋势。

## 素材源优先级

素材源决定文章质量下限，但**信息质量 > 信源等级**。按以下优先级选择写入素材：

**S级（优先使用）：** 大厂报告/白皮书（Google、Microsoft、Anthropic、OpenAI、Meta、Amazon、Apple）、顶级咨询公司报告（McKinsey、BCG、Bain、Deloitte、PwC、Accenture、KPMG）、顶级学术论文（Nature、Science、NeurIPS、ICML）、权威媒体深度报道（NYT、WSJ、FT、Bloomberg、The Atlantic、New Yorker的长报道/调查报道）。

**A级（可用）：** 行业垂直研究（Gartner、Forrester、IDC）、知名投资机构分析（a16z、Sequoia、Index Ventures）、技术博客（Google AI、Meta AI、OpenAI Blog、Anthropic Blog）、可信中英文媒体的分析/观点文章。

**B级（可用，段位高于一切时）：** 一般媒体报道、个人博客、社区讨论（Reddit/HN 等）。**若社区素材含一手实施经验、真实踩坑、可核实的细节，且能支撑高段位判断（维度2标准），允许以其为核心素材**——一手经验的洞察密度往往高于二手转述。

**C级（跳过）：** 来源不明的自媒体、纯翻译/搬运内容、AI摘要生成的文章。

写作时至少有一个核心素材是 S 级或 A 级；**若 B 级素材洞察独到、细节扎实，可豁免此条**。

## AI 安全白名单

**默认禁止**以下 AI 安全相关话题作为主选题：
- AI 对齐（alignment）/ 价值观对齐
- AI 安全（safety）/ 红队测试（red teaming）
- AI 监管 / 政策法规
- AI 偏见 / 公平性 / 歧视
- AI 可解释性（interpretability / explainability）
- AI 风险 / 存在风险 / 毁灭风险
- AI 治理框架 / 评估框架（如 Anthropic 的 safety frameworks）

**例外条件（满足全部三条才可写）：**
1. 观点与主流认知明显不一致（不是换个角度，是真不一样）
2. 包含实验/事件/数据支撑，不是纯观点陈述
3. 有具体的行为结果，不是风险可能性讨论

**可接受的例子：**「Agent 之间在真实生产环境中相互攻击，导致系统崩溃，并有完整复现实验和攻击链文档」——因为有实验、有具体行为、有人类未曾预料的结果。

**不可接受的例子：**「AI 安全框架需要迭代」「模型对齐仍然存在根本性挑战」——没有新信息、没有具体事件。

## 写作目标（两条路径通用）
1. 让读者理解一个正在发生的变化
2. 解释变化背后的机制
3. 说明变化意味着什么——**决策者含义必须内嵌在机制论证里，作为论证的自然结果出现，禁止单独成段**
4. 主动处理一个常见误读或边界
5. 留下一个可复述的判断——**可迁移的普适判断，不是"这个案例的结论"**

## 收尾规则（防 AI slop）

**禁止**以固定模板收尾。以下结构一律不许出现：
- ❌ "对企业的启示" / "对管理者的建议" / "给老板的三点建议" + 列表
- ❌ "决策者该引入的流程" / "给一条可操作路径" / "建议企业如何做" + 行动清单（决策含义独立成章也不行）
- ❌ "这意味着什么" / "这说明什么" / "这背后的逻辑是" + 总结段
- ❌ "写在最后" / "总而言之" / "展望未来" + 全文回顾
- ❌ 最后一段回顾全文论点再升华

**正确做法**：
- 决策者含义（该做什么/该防什么）是论证的自然结果，**内嵌在讲机制的段落里**——比如讲完代价结构，一句"这类错误的代价是非对称的"本身就是决策含义，不需要再展开"所以企业应该……"
- 文章最后一节仍然推进机制或边界，写完即止，不切换成"给建议"模式
- 若要有收官句，用判断句直接收束——一句新的判断，不是对前文的总结

## 工作流程（必须在脑中执行，不得输出到文章中）

**这是你的内部思考步骤，不是文章大纲。你的输出只有最终的完整文章，不包含任何思考过程和中间产出。**

在脑中严格按顺序执行以下步骤，但只输出第 9 步的结果（完整文章）：

1. 路径判定：判断当前素材适合路径 A 还是 B。
2. 信息抽取：提取核心事实、数据、案例、结论。
3. 异常信号识别：找 2-4 个"咦"信号。
4. 三层萃取：表层结论→中层机制→深层趋势。**升维标准：提炼出对同类企业普遍成立、对决策者有行动含义的判断**——问自己"这个 case 背后的机制是什么？换成另一家企业结论还成立吗？老板看完能做什么决定？"。
5. 核心判断生成：生成 2-4 个候选判断（淘汰无机制、无边界、可套用的；保留可迁移、对决策者有含义的）。
6. 情报扫描：搜索已有讨论和反方观点。
7. 反证与边界检查：找反证，明确适用边界。
8. 推理链设计：设计递进推理链（有证据等级）。
9. 正文写作：选择章节结构，按下面的写作要求展开。**每个章节围绕"机制/判断"组织，case 细节只作为证据出现，不要让素材的原始叙述顺序主导文章结构。**
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

def _safe_str(v):
    """Defensive: coerce to a stripped non-empty string or empty string. Never None."""
    if v is None:
        return ""
    if isinstance(v, list):
        v = "；".join(str(x) for x in v if x is not None)
    return str(v).strip()


def _build_structured_material(m: dict) -> dict:
    """Normalize one material into the structured research format (方案 4.1).

    Loads whatever structured fields already exist on the material / tag DB,
    then falls back to heuristics over the raw text. Never crashes on missing
    fields — the goal is a *compatible, enriched* context, not a rewrite.
    """
    import re as _re
    url = (m.get("url") or m.get("canonical_url") or "") or ""
    title = _safe_str(m.get("title") or url)
    summary = _safe_str(m.get("summary") or m.get("digest"))
    content = m.get("content") or ""
    key_signal = _safe_str(m.get("key_signal"))

    # ── Prefer existing structured fields if present ──
    facts = m.get("facts") or []
    data_points = m.get("data_points") or []
    mechanisms = m.get("mechanisms") or []
    author_judgments = m.get("author_judgments") or []
    counterpoints = m.get("counterpoints") or []
    boundaries = m.get("boundaries") or []
    source_grade = _safe_str(m.get("source_grade") or m.get("source_quality") or "unknown")

    # ── Reuse tag DB when available (research cards / tag_materials) ──
    if not (facts or data_points or mechanisms or author_judgments) and content:
        try:
            from lib.materials import _load_material_tags
            tags_db = _load_material_tags()
            entry = tags_db.get(url.rstrip("/"), {})
            if not key_signal:
                key_signal = _safe_str(entry.get("key_signal"))
            tags = entry.get("tags") or {}
            for dim, tag in tags.items():
                tag = _safe_str(tag)
                if not tag or len(tag) < 4:
                    continue
                diml = str(dim).lower()
                if any(k in diml for k in ("事实", "fact", "数据", "data")):
                    facts.append(tag)
                elif any(k in diml for k in ("机制", "mechan", "judg", "观点", "判断")):
                    if any(k in diml for k in ("judg", "观点", "判断")):
                        author_judgments.append(tag)
                    if any(k in diml for k in ("机制", "mechan")):
                        mechanisms.append(tag)
                elif any(k in diml for k in ("结论", "insight", "key", "signal")):
                    if not key_signal:
                        key_signal = tag
        except Exception:
            pass

    # ── Light heuristic extraction from the leading part of the body ──
    if not facts and content:
        # pull concrete numbers/% out of the material as data points
        for mt in _re.finditer(r'(\d{1,3}(?:[\.,]\d+)?\s*%|\d+\s*(?:亿|万|B|M|K)|\$\s?\d+)', content):
            data_points.append({"claim": mt.group(0), "value": mt.group(0), "source": url})

    facts = [str(x) for x in facts if x][:6]
    data_points = [x for x in data_points if isinstance(x, dict)][:6]
    mechanisms = [str(x) for x in mechanisms if x][:5]
    author_judgments = [str(x) for x in author_judgments if x][:5]
    counterpoints = [str(x) for x in counterpoints if x][:3]
    boundaries = [str(x) for x in boundaries if x][:3]

    return {
        "title": title,
        "source_url": url,
        "source_grade": source_grade,
        "summary": summary,
        "facts": facts,
        "data_points": data_points,
        "mechanisms": mechanisms,
        "author_judgments": author_judgments,
        "counterpoints": counterpoints,
        "boundaries": boundaries,
        "key_signal": key_signal,
    }


def build_material_context(materials: list[dict]) -> str:
    """Build a structured research-card context from materials (方案 4.1).

    Injects each material as a structured "素材研究卡" (facts / data / mechanism /
    judgment / counterpoint / boundary / source), NOT a blind 3000-char truncation.
    Long raw bodies are attached only as constrained supporting detail, so the
    writer still has evidence but is not drowning in multiple article *openings*.
    """
    parts = []
    for i, m in enumerate(materials, 1):
        card = _build_structured_material(m)
        body = m.get("content") or ""
        lines = [
            f"=== 素材 {i} ===",
            f"标题: {card['title']}",
            f"来源: {card['source_url']}   (级别: {card['source_grade']})",
        ]
        if card["summary"]:
            lines.append(f"摘要: {card['summary']}")
        if card["key_signal"]:
            lines.append(f"独特信号: {card['key_signal']}")
        if card["facts"]:
            lines.append("可核验事实: " + "；".join(card["facts"]))
        if card["data_points"]:
            dp = []
            for d in card["data_points"]:
                if isinstance(d, dict):
                    claim = _safe_str(d.get("claim") or d.get("value"))
                    src = _safe_str(d.get("source"))
                    dp.append(f"{claim}(源:{src})" if src else claim)
                else:
                    dp.append(str(d))
            lines.append("数据点: " + "；".join(dp))
        if card["mechanisms"]:
            lines.append("机制链: " + "；".join(card["mechanisms"]))
        if card["author_judgments"]:
            lines.append("原作者判断: " + "；".join(card["author_judgments"]))
        if card["counterpoints"]:
            lines.append("反方/限制: " + "；".join(card["counterpoints"]))
        if card["boundaries"]:
            lines.append("适用边界: " + "；".join(card["boundaries"]))
        # Constrained body excerpt for depth (still capped, but each section is
        # labeled so the writer knows what it is, instead of a wall of openings).
        if body:
            lines.append(f"\n原文摘录(前{1900}字):\n" + str(body)[:1900].replace("\n", " "))
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


BLUEPRINT_SYSTEM_PROMPT = """你是一个公众号文章的"蓝图设计师"。你只负责产出文章蓝图（结构化 JSON），不写正文。

素材可能包含：事实、数据、机制、作者判断、反方观点、适用边界。你必须基于这些素材做独立判断，而不是复述素材标题。

## 输出要求
只输出一个 JSON 对象（不要任何前后缀文字、不要 markdown 代码块），结构如下：
{
  "topic": "文章主题",
  "thesis": "一句可复述的核心判断（必须有明确立场，禁止'AI正在快速发展'这类泛判断）",
  "thesis_transferability": "该判断对哪类企业/场景普遍成立（脱离当前素材也能成立）",
  "why_now": "为什么现在值得写",
  "evidence": ["支持判断的事实/数据/案例及来源"],
  "mechanism_chain": ["现象", "中间机制", "结果/影响"],
  "counterargument": "最强反方观点",
  "boundary": "适用边界或不成立的情况",
  "reader_consequence": "对企业管理者/读者的具体影响",
  "outline": ["章节判断1", "章节判断2", "章节判断3"],
  "title_candidates": ["标题1", "标题2", "标题3"],
  "source_map": [{"claim": "判断", "source_urls": ["URL"]}]
}

## 硬约束
- thesis 必须能脱离素材被一句话复述，且机制链至少 2 个环节。
- mechanism_chain 明说 因果/传导，禁止只堆现象。
- evidence 至少 2 条真实可追溯（来自上面素材）；没有的话，明确标注"证据不足"，且 data 字段留空，绝不允许编造数字。
- 必须给 counterargument 和 boundary（反方/边界至少一个真实存在）。
- outline 是"推进判断的章节"，不是对素材标题的改写。
- 所有素材必须属于同一核心议题；若素材跨主题，topic 只能聚焦其中一个，其余在 counterargument/boundary 中处理，禁止跨主题拼接。
- 若单一论点素材不足以支撑深度，输出 {"insufficient": true, "reason": "..."}。"""


def build_blueprint_prompt(context: str, angle_instruction: str) -> list[dict]:
    """Stage-1 prompt: produce ONLY a blueprint JSON, no article body."""
    user = (
        "素材（结构化研究卡）：\n"
        f"{context}\n\n"
        f"{angle_instruction}\n"
        "请基于以上素材，生成文章蓝图 JSON。只输出 JSON 对象本身，不要解释、不要正文。"
    )
    return [
        {"role": "system", "content": BLUEPRINT_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


BLUEPRINT_HARD_FAIL_MSG = (
    "⚠️ 蓝图未通过校验，不进入正文写作。请补足后重试：{reasons}"
)


def validate_blueprint(bp: dict) -> dict:
    """Validate a blueprint against 方案 4.3 criteria.

    Returns {"ok": True} or {"ok": False, "reasons": [...]}.
    """
    reasons = []
    if "insufficient" in bp and bp.get("insufficient"):
        return {"ok": False, "reasons": ["素材不足：不建议成文/需要补充材料", str(bp.get("reason") or "")]}

    thesis = _safe_str(bp.get("thesis"))
    if not thesis or len(thesis) < 8:
        reasons.append("thesis 缺失或过短")
    # 泛判断拦截
    _ban = ["AI正在快速发展", "AI快速发展", "数字化是大势所趋", "人工智能正在改变世界",
            "AI已经成为趋势", "时代在变化"]
    if thesis and any(b in thesis for b in _ban):
        reasons.append("thesis 是泛判断")

    mechanism = bp.get("mechanism_chain") or []
    if not isinstance(mechanism, list) or len(mechanism) < 2 or not any(_safe_str(x) for x in mechanism):
        reasons.append("机制链 mechanism_chain 少于2个环节")

    evidence = bp.get("evidence") or []
    if not isinstance(evidence, list) or len([e for e in evidence if _safe_str(e)]) < 2:
        reasons.append("evidence 少于2条可追溯证据")

    if not _safe_str(bp.get("counterargument")) and not _safe_str(bp.get("boundary")):
        reasons.append("缺少反方观点或边界")

    outline = bp.get("outline") or []
    if isinstance(outline, list):
        if not outline or not any(_safe_str(o) for o in outline):
            reasons.append("outline 为空")

    return {"ok": not reasons, "reasons": reasons}


def _safe_source_map(blueprint) -> list:
    """Extract a safe list of {claim, source_urls} from a blueprint dict."""
    if not isinstance(blueprint, dict):
        return []
    src_map = blueprint.get("source_map")
    if not isinstance(src_map, list):
        return []
    out = []
    for sm in src_map:
        if isinstance(sm, dict):
            claim = _safe_str(sm.get("claim"))
            urls = [u for u in (sm.get("source_urls") or []) if isinstance(u, str) and u]
            if claim or urls:
                out.append({"claim": claim, "source_urls": urls})
    return out


def extract_blueprint(response: str) -> dict:
    """Parse blueprint JSON from LLM response, tolerating code fences / stray text."""
    import json as _json
    if response is None or not response.strip():
        return {}
    text = response.strip()
    # strip markdown code fence if present
    import re as _re
    fence = _re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, _re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    else:
        # take from first { to last }
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start:end + 1]
    try:
        obj = _json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


# ── 文章质量闸门（纯 Python，无副作用；方案 4.5）──

def check_article_quality(
    article_text: str,
    title: str = "",
    source_map: Optional[list] = None,
) -> dict:
    """Pure, side-effect-free quality gate.

    8 checks; hard gates = has_thesis, has_mechanism, no_empty_content.
    Rules are only the FIRST pass — the main agent does final acceptance.
    Returns {"passed", "score", "checks", "warnings", "reasons"}.
    """
    import re as _re
    text = article_text or ""
    reasons: list[str] = []
    warnings: list[str] = []

    has_content = bool(text.strip())
    # 1. no_empty_content (hard)
    if not has_content or len(text.strip()) < 200:
        has_content = False

    # 2. has_thesis (hard): a judgment sentence exists (not a pure question)
    sentences = [s.strip() for s in _re.split(r'[。!?！？\n]', text) if s.strip()]
    judgment_sentences = [s for s in sentences if len(s) >= 8 and not s.startswith(("为什么", "如何", "是不是", "吗"))]
    has_thesis = len(judgment_sentences) >= 1

    # 3. has_mechanism (hard): looks for causal chain markers
    mechanism_markers = ["因为", "导致", "所以", "从而", "这意味着", "因此", "传导", "机制", "链", "后果", "推动", "转化为", "从而使得"]
    has_mechanism = any(m in text for m in mechanism_markers)

    # 4. has_evidence: specific numbers or explicit claim
    has_numbers = bool(_re.search(r'\d+\s*[%万亿]|\d+\.\d+|\$\s?\d+|\d+\s*(人|家|个|倍)', text))
    evidence_claims = ["数据", "调查", "报告", "实验", "案例", "抽样", "样本", "研究"]
    has_evidence = has_numbers or any("".join(k) in text for k in evidence_claims)

    # 5. has_counterpoint_or_boundary
    boundary_markers = ["但", "不过", "然而", "局限", "边界", "不适用", "例外", "反例", "另一方面", "代价", "前提", "并非"]
    has_counterpoint_or_boundary = any(m in text for m in boundary_markers)

    # 6. has_source_traceability
    has_url = bool(_re.search(r'https?://', text)) or bool(_re.search(r'来源[:：]', text))
    sources_present = source_map and any(
        (sm.get("source_urls") or []) for sm in source_map) if isinstance(source_map, list) else False
    has_source_traceability = has_url or sources_present

    # 7. not_material_dump: paragraphs aren't just verbatim quotes / list replays
    paragraphs = [p for p in _re.split(r'\n\s*\n', text) if p.strip()]
    verbatim_count = 0
    for p in paragraphs:
        p_stripped = p.strip()
        if p_stripped.startswith(("=== 素材", "原文摘录", "• ")) or len(p_stripped) < 8:
            verbatim_count += 1
    not_material_dump = (len(paragraphs) == 0) or (verbatim_count / max(len(paragraphs), 1) < 0.5)

    # 8. title_is_judgment
    if title:
        ban_titles = ["报告解读", "深度分析", "几点思考", "发展趋势", "数字化转型的思考", "AI的发展"]
        title_is_judgment = not any(b in title for b in ban_titles) and len(title) >= 6
    else:
        first_heading = _re.search(r'^#\s+(.+)', text, _re.MULTILINE)
        title_is_judgment = bool(first_heading) and len(first_heading.group(1).strip()) >= 6

    checks = {
        "has_thesis": bool(has_thesis),
        "has_mechanism": bool(has_mechanism),
        "has_evidence": bool(has_evidence),
        "has_counterpoint_or_boundary": bool(has_counterpoint_or_boundary),
        "has_source_traceability": bool(has_source_traceability),
        "not_material_dump": bool(not_material_dump),
        "title_is_judgment": bool(title_is_judgment),
        "no_empty_content": bool(has_content),
    }
    passed_count = sum(1 for v in checks.values() if v)

    hard_fail = not (checks["has_thesis"] and checks["has_mechanism"] and checks["no_empty_content"])
    passed = (not hard_fail) and passed_count >= 6

    if not checks["no_empty_content"]:
        reasons.append("文章为空或过短")
    if not checks["has_thesis"]:
        reasons.append("缺少可复述的核心判断")
    if not checks["has_mechanism"]:
        reasons.append("缺少机制链")
    if passed_count < 6:
        reasons.append(f"通过 {passed_count}/8，低于 6 项门槛")

    return {
        "passed": passed,
        "score": passed_count,
        "checks": checks,
        "warnings": warnings,
        "reasons": reasons,
    }


# ── 完整质量闸门 = 规则硬门槛 + LLM 语义评估（方案升级）──
# 兼容保留 check_article_quality（纯规则，老调用方不变）。
# evaluate_article_quality 在其上叠加 LLM 语义复核：
#   规则硬门槛不过 -> 直接不可通过；
#   规则过 -> 用 lib.quality_judge 调 DeepSeek 直连做 8 维语义评估；
#   任何不可用/失败 -> 按不可通过处理，绝不伪造通过。
def evaluate_article_quality(
    article_text: str,
    title: str = "",
    source_map: Optional[list] = None,
    blueprint_text: str = "",
    enable_llm_judge: bool | None = None,
    model: str | None = None,
) -> dict:
    """规则硬门槛 + LLM 语义评估 的完整质量闸门。

    默认行为：若环境变量 QUALITY_LLM_JUDGE 为真值（1/true/on）且凭证可用则启用 LLM 语义评估；
    否则仅规则层（degraded 模式，绝不伪造通过）。
    返回 dict 字段与 check_article_quality 兼容，另含 rules/llm/status。
    """
    if enable_llm_judge is None:
        enable_llm_judge = os.environ.get("QUALITY_LLM_JUDGE", "").strip().lower() in ("1", "true", "yes", "on")
    if not enable_llm_judge:
        # degraded：仅规则层，显式可见，不做语义复核（不会伪造通过）。
        base = check_article_quality(article_text, title=title, source_map=source_map)
        base["status"] = "rules"
        base["rules"] = dict(base)
        base["llm"] = None
        return base
    try:
        from lib.quality_judge import evaluate_quality
    except Exception:
        base = check_article_quality(article_text, title=title, source_map=source_map)
        base["status"] = "rules"
        base["rules"] = dict(base)
        base["llm"] = None
        return base
    return evaluate_quality(
        article_text,
        title=title,
        source_map=source_map,
        blueprint_text=blueprint_text,
        model=model,
    )


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


def _extract_placeholder_alt_text(content: str) -> dict[int, str]:
    """Map image index (1-based) to its placeholder alt/description text.

    Reads both `![alt](./images/image-NNN-...)` and legacy `<!-- IMAGE: N -->`
    forms. Returns {index: alt_text}. Used to feed Pexels-first body-image
    retrieval with per-placeholder captions.
    """
    out: dict[int, str] = {}
    # Primary: ![alt](./images/image-NNN-xxx...)
    for m in re.finditer(r'!\[([^]]*)\]\(\./images/image-(\d{3})', content):
        alt = m.group(1).strip()
        idx = int(m.group(2))
        if alt and idx not in out:
            out[idx] = alt
    # Legacy: <!-- IMAGE: N -->
    for m in re.finditer(r'<!--\s*IMAGE\s*:\s*(\d+)\s*-->', content, re.IGNORECASE):
        idx = int(m.group(1))
        if idx not in out:
            out[idx] = "配图"
    return out


def count_image_placeholders(content: str) -> int:
    """Count image placeholders in content. Supports both formats."""
    # Primary: <!-- IMAGE: N --> (legacy support)
    count1 = len(re.findall(r'<!--\s*IMAGE\s*:\s*\d+\s*-->', content, re.IGNORECASE))
    # Secondary: ![desc](./images/image-NNN-xxx.jpeg)
    count2 = len(re.findall(r'!\[[^\]]*\]\(\./images/image-\d{3}-', content))
    # Prefer ![]() count if any found; fall back to legacy
    return count2 if count2 > 0 else count1


def run_image_search(
    query: str,
    output_dir: str,
    index: int = 1,
    description: str = "",
    exclude_ids: Optional[set[str] | frozenset[str]] = None,
) -> Optional[str]:
    """Resolve one placeholder with a Pexels-first policy.

    With PEXELS_FIRST enabled (default), tries the reusable, in-process resolver
    (lib.pexels_images.resolve_image) which searches Pexels using the placeholder
    `description` and post-processes the photo. Only when that yields nothing (or
    PEXELS_FIRST is disabled) does it fall back to the legacy image_search.py
    subprocess (Pexels -> Pixabay -> local placeholder), so the article still
    renders a real file.

    ``exclude_ids``: Pexels photo IDs already chosen for EARLIER placeholders in
    this SAME article. They are merged with the persistent history ledger inside
    lib.pexels_images, so we do not stock two different placeholders in one
    article with the same stock photo.

    Returns the chosen Pexels photo ID on success (best-effort; '' if unknown),
    else '' on a degraded-but-usable result, and None on failure.
    """
    # R1: skip remaining image searches when the global budget is nearly
    # exhausted. We guard here (in addition to image_search.py itself) so we
    # don't even spawn the subprocess - the writer then finishes and publishes
    # instead of being killed at the 900s parent limit.
    try:
        _img_deadline = float(os.environ.get("IMAGE_SEARCH_DEADLINE", "0"))
    except ValueError:
        _img_deadline = 0.0
    if _img_deadline and time.time() > _img_deadline - 60:
        print(f"  ⏰ Image-search budget nearly exhausted; skipping '{query}' (placeholder fallback)")
        return None

    images_dir = Path(output_dir)
    images_dir.mkdir(parents=True, exist_ok=True)
    expected = images_dir / f"image-{index:03d}-pexels.jpg"

    # Pexels-first, in-process resolver (reusable module). Honors PEXELS_FIRST.
    # Prefer a per-placeholder description-derived query (more specific English
    # keywords for the caption); fall back to the caller's whole-article query.
    if px.PEXELS_FIRST:
        placeholder_query = px._build_query(description, filename_hint=f"image-{index:03d}-{query}") \
            if description and description != query else ""
        search_q = placeholder_query or query or "enterprise business technology"
        res = px.resolve_image(
            expected,
            description=description,
            query=search_q,
            filename_hint=f"image-{index:03d}-{query}" if query else "",
            output_dir=images_dir,
            allow_generative=False,   # body images: never silently generate here
            gen_fallback=None,
            long_edge=px.DEFAULT_LONG_EDGE,
            exclude_ids=exclude_ids,
        )
        if res.ok and res.path and res.path.exists():
            return res.photo_id or ""
        print(f"  ⚠️ Pexels-first no hit for '{search_q}': {res.reason}")

    # Legacy subprocess fallback (Pexels -> Pixabay -> local placeholder).
    if not IMAGE_MATCHER_SCRIPT.exists():
        print(f"  ⚠️ image_search.py not found: {IMAGE_MATCHER_SCRIPT}")
        return None
    cmd = [
        sys.executable, str(IMAGE_MATCHER_SCRIPT),
        query,
        output_dir,
        "--index", str(index),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            import glob
            image_files = glob.glob(os.path.join(output_dir, f"image-{index:03d}-*"))
            return "" if image_files else None
        if result.stderr:
            print(f"    ⚠️ {result.stderr.strip()}")
        return None
    except subprocess.TimeoutExpired:
        print(f"  ⚠️ image_search.py timed out")
        return None

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
    # R1: phase timing anchors for the segmented budget log (written at the end).
    t_write_start = time.time()
    t_polish_start = None
    t_img_start = None

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

        # 当日选题器(runtime.py)已经通过内容哈希去重选好素材，直接信任这份候选：
        # 不再用 all_urls.tsv 的历史 used 状态二次过滤。否则若这批当日素材里
        # 恰好有历史 used 标记，会被再次过滤为空 → 整篇文章产出失败。
        # 跨天去重由上游 content-hash dedup + sample_materials 的近 3 天
        # recent_used_urls 过滤共同保证，这里移除 redundant 的 legacy 过滤。
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
    sampled = sample_materials(enriched, max_materials, skip_used_filter=bool(materials_override))
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
    # 修正：实际用的模型来自 --model / lib.models.get_model("writing")，如实打印
    print(f"\n6️⃣  调用写作模型 {model} 生成文章...")
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
            "\n"
            "## 内容深度检查清单\n"
            "\n"
            "写作前，隐去所有素材，仅凭记忆问自己：\n"
            "1. 这篇文章的核心判断是什么？一句话说得清吗？\n"
            "2. 这个判断有机制解释吗（不仅仅是观察到现象，而是解释了为什么）？\n"
            "3. 如果我是读者，读完后能告诉我朋友什么？\n"
            "4. 这句判断换到半年前是否也成立？如果是，说明没有增量信息。\n"
            "\n"
            "然后做**情报扫描**：搜索当前讨论中已经有哪些类似观点，确保文章不是跟在别人后面说一样的话。只有当你的判断提供了不一样的东西（新机制、反常识、边界条件、具体案例）时才值得写。\n"
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

    # ══════════════════════════════════════════════════════════════
    # 阶段 1：文章蓝图（方案 4.3）— 只生成蓝图，不写正文
    # ══════════════════════════════════════════════════════════════
    print("\n7️⃣  阶段1：生成文章蓝图...")
    blueprint_messages = build_blueprint_prompt(context, angle_instruction)
    bp_response = call_model(blueprint_messages, temperature=0.5, max_tokens=2048, model=model)
    if not bp_response:
        print("  ❌ 蓝图阶段模型无响应（不进入正文写作）")
        return None
    blueprint = extract_blueprint(bp_response)
    if not blueprint:
        print("  ❌ 蓝图无法解析为 JSON（不进入正文写作）")
        print(f"     响应预览: {bp_response[:400]}")
        return None
    bp_check = validate_blueprint(blueprint)
    if not bp_check["ok"]:
        print(f"  ❌ 蓝图未通过校验，不进入正文写作:")
        for r in bp_check["reasons"]:
            print(f"       - {r}")
        # 记录失败原因以便人工复核；不生成空文章、不入发布队列
        print("  ⛔ 输出：不建议成文 / 待人工补足素材后重试")
        return None
    print(f"  ✓ 蓝图通过校验。核心判断: {_safe_str(blueprint.get('thesis'))[:50]}...")
    _bp_json = json.dumps(blueprint, ensure_ascii=False, indent=2)
    blueprint_context = f"""
=== 已通过的写作蓝图（必须严格执行） ===
{_bp_json}
"""

    user_prompt = f"""你已经有了经过校验的写作蓝图。请严格按照蓝图的 thesis / mechanism_chain / counterargument / boundary / outline 撰写完整正文。

{blueprint_context}
以下是我从信息源收集到的素材（结构化研究卡）。写作时必须服务于上面的 thesis，再安排素材；不要按原报告目录复述；不得补写素材中不存在的数字、案例、引语；对不确定内容使用"材料未能证明"，不要伪装成事实。

1. 每节先推进判断，再解释证据和机制
2. 至少有一处处理反方观点或边界
3. 结尾回到核心判断，不用泛泛口号
4. 标题必须表达判断，不得只是主题标签
5. 用 Markdown 图片格式 `![描述](./images/image-NNN-xxx.jpeg)` 插入占位符
6. 直接输出最终 Markdown 文章，以 # 标题开头，正文中禁止出现 T1/T2/T3/T4、ABC 分级等内部流程标记

{angle_instruction}素材内容：
{context}

注意：直接输出完整的 Markdown 文章，以 # 标题开头。如果素材让你想到的判断与最近文章类似，一定要主动换方向。

⚠️ 强制要求：你必须从以上素材中提取具体的数据、案例、判断来支撑文章论点。如果素材中没有对应的信息，不要自行杜撰数据或虚构案例。文章中必须有至少 2 处引用素材中的具体信息。"""
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

    response = call_model(messages, temperature=1.0, max_tokens=None, model=model)
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

    # ══════════════════════════════════════════════════════════════
    # 文章质量闸门（方案 4.5）— 不通过则保留为待修草稿，不入发布队列
    # ══════════════════════════════════════════════════════════════
    quality = evaluate_article_quality(
        article,
        title=title,
        source_map=_safe_source_map(blueprint),
        blueprint_text=json.dumps(blueprint, ensure_ascii=False) if isinstance(blueprint, dict) else "",
    )
    _qc = quality["checks"]
    _qstatus = quality.get("status", "rules")
    print(f"\n🔍 质量闸门: {'✅ 通过' if quality['passed'] else '❌ 未通过'} "
          f"(得分 {quality['score']}/8, 模式 {_qstatus})")
    for k in ("has_thesis", "has_mechanism", "has_evidence", "has_counterpoint_or_boundary",
              "has_source_traceability", "not_material_dump", "title_is_judgment", "no_empty_content"):
        print(f"     {'✓' if _qc[k] else '✗'} {k}")
    if quality.get("llm") and _qstatus != "rules":
        _llm = quality["llm"]
        print(f"     [LLM语义评估] verdict={_llm.get('verdict')} "
              f"overall={_llm.get('overall_score')}/5: {_llm.get('overall_reason')}")
        for _d, _v in (quality["llm"].get("dimensions") or {}).items():
            print(f"        · {_d}: {_v.get('score')}/5 — {_v.get('reason')}")
    for r in quality["reasons"]:
        print(f"     ⚠️ {r}")

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
        meta_path.write_text(json.dumps({"digest": digest}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  ✓ Digest: {digest}")
    else:
        print(f"  ⚠️ No digest extracted (LLM didn't output '摘要:' line)")

    # ── Persist blueprint + raw response + quality gate (方案 4.3/4.5) ──
    # These artifacts let a human/agent audit why this article was written and
    # whether the gate rejected/approved it. Never deleted; no publish side-effect.
    try:
        bp_path = output_dir / "blueprint.json"
        bp_path.write_text(json.dumps(blueprint, ensure_ascii=False, indent=2), encoding="utf-8")
        raw_path = output_dir / "_blueprint_raw_response.txt"
        raw_path.write_text(bp_response or "", encoding="utf-8")
        # Merge gate decision into meta.json (keeps digest too)
        meta_path = output_dir / "meta.json"
        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                meta = {}
        meta["quality"] = {
            "passed": quality["passed"],
            "score": quality["score"],
            "checks": quality["checks"],
            "reasons": quality["reasons"],
            "warnings": quality["warnings"],
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        # Standalone gate report too
        qr_path = output_dir / "quality_report.json"
        qr_path.write_text(json.dumps(quality, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  📋 蓝图 + 质量报告已保存: {output_dir}")
    except Exception as _e:
        print(f"  ⚠️ 保存蓝图/质量报告失败: {_e}")

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
        # 2026-08-02 修复 OlmoEarth 连续重选题根因：
        # 之前 mark_source_urls=not bool(materials_override)，而每日管线
        # runtime.py 恒以 --materials 调用本文，导致 materials_override=True
        # → 永不为 False → 从不在 all_urls.tsv 标记 used →
        # sample_materials 的 filter_used_materials 永远拦不住已用素材。
        # 已用素材无论以何种方式写入都应标记 used，因此恒为 True。
        mark_source_urls=True,
    )

    # Step 6: Polish article with OpenAI via OpenRouter
    # Done BEFORE image matching — text must be finalized first
    t_polish_start = time.time()
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
    t_img_start = time.time()
    print("\n7️⃣  Matching images...")
    num_placeholders = count_image_placeholders(article)
    print(f"  Found {num_placeholders} image placeholders")

    if num_placeholders > 0:
        queries = generate_image_search_queries(article, num_placeholders)
        # Map placeholder description (alt text) per image index so the Pexels-first
        # resolver can search using the actual caption rather than a generic query.
        placeholder_descs = _extract_placeholder_alt_text(article)
        # Track Pexels photo IDs already chosen for earlier placeholders in THIS
        # article so we never put the same stock photo into two slots (dedup).
        used_photo_ids = set()
        for i, query in enumerate(queries[:num_placeholders + 1], 1):
            desc = placeholder_descs.get(i, "")
            print(f"  Searching image {i}/{min(len(queries), num_placeholders + 1)}: '{query}'"
                  + (f" (desc: {desc[:24]}...)" if desc else ""))
            chosen = run_image_search(query, str(images_dir), i, description=desc,
                                      exclude_ids=used_photo_ids)
            if chosen is not None:
                if chosen:
                    used_photo_ids.add(chosen)
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
        used_photo_ids = set()
        for i, query in enumerate(default_queries[:4], 1):
            print(f"  Searching image {i}: '{query}'...")
            chosen = run_image_search(query, str(images_dir), i,
                                      exclude_ids=used_photo_ids)
            if chosen:
                used_photo_ids.add(chosen)

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

    # List output
    print(f"\n  ✅ Output: {output_dir}/")
    for f in sorted(output_dir.iterdir()):
        if f.is_dir():
            print(f"     📁 images/ ({len(list(f.iterdir()))} files)")
        else:
            print(f"     📄 {f.name}")

    # ── R1: segmented timing log for budget observability ──
    t_end = time.time()
    _polish_anchor = t_polish_start or t_end
    _img_anchor = t_img_start or t_end
    write_dur = _polish_anchor - t_write_start
    polish_dur = _img_anchor - _polish_anchor
    img_dur = t_end - _img_anchor
    total_dur = t_end - t_write_start
    print(f"\n⏱️  Pipeline timing — write: {write_dur:.1f}s | polish(+post): "
          f"{polish_dur:.1f}s | image+embed: {img_dur:.1f}s | TOTAL: {total_dur:.1f}s")
    if total_dur > 800:
        print(f"  ⚠️ near budget: total {total_dur:.1f}s > 800s; "
              f"consider tightening LLM/image stages for headroom")

    return str(output_dir)


def enqueue_for_publish(output_dir: str, digest: str = "") -> bool:
    """Compatibility wrapper around the shared, lock-protected queue."""
    from publish_queue import enqueue_for_publish as _enqueue

    return _enqueue(output_dir, digest)


def main():
    parser = argparse.ArgumentParser(description="Write article from collected materials")
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
    parser.add_argument("--no-publish", action="store_true",
                        help="Skip enqueueing the article for async WeChat publishing (local testing)")
    args = parser.parse_args()

    # R1: compute ONE global budget here and inject it as env vars so every
    # spawned subprocess (image_search.py, polish, embed) and lib/llm.call_model
    # share the same clock. 840s leaves ~60s headroom under the 900s parent.
    PIPELINE_DEADLINE = time.time() + 840
    os.environ["LLM_DEADLINE"] = str(PIPELINE_DEADLINE)
    os.environ["IMAGE_SEARCH_DEADLINE"] = str(PIPELINE_DEADLINE)

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
        # Stage A ends at the queue boundary. An independently scheduled worker
        # consumes the record and creates a WeChat draft; the writer never
        # starts or waits for the sender.
        if not args.no_publish and not args.dry_run:
            digest = ""
            quality_passed = True
            meta_path = Path(output_dir) / "meta.json"
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    digest = meta.get("digest", "")
                    q = meta.get("quality") or {}
                    quality_passed = bool(q.get("passed", True))
                except (json.JSONDecodeError, OSError):
                    pass
            # ⛔ 质量闸门（方案 4.5）：未通过的文章不自动进入发布队列。
            if not quality_passed:
                print("  ⛔ 质量闸门未通过 — 不进入发布队列，文章已作为待修草稿保留")
                print(f"     Output preserved (待人工审核/补素材): {output_dir}")
            elif enqueue_for_publish(output_dir, digest):
                print(f"  📋 Queued for async publish (draft): {QUEUE_DIR / 'pending.jsonl'}")
                print("  ⏳ Independent publish worker will consume the queue")
            else:
                print(f"  ⚠️ Failed to enqueue for publish (see warnings above)")

        print(f"\n{'='*60}")
        print(f"✅ Article pipeline complete!")
        print(f"   Output: {output_dir}")
        print(f"{'='*60}")
        return 0
    else:
        print(f"\n{'='*60}")
        print(f"❌ Article pipeline failed")
        print(f"{'='*60}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
