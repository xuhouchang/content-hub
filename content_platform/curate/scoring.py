"""Score materials for editorial fit using LLM judgment.

Replaces keyword-based scoring with LLM evaluation across five dimensions:
  1. 主题相关度 (Topic Relevance)        — 30%
  2. 决策者洞察价值 (Executive Insight)  — 35%
  3. 信息密度 (Information Density)      — 15%
  4. 写作价值 (Writing Value)            — 10%
  5. 信源等级 (Source Authority)         — 10%

v6 变更：维度2 从「决策者洞察价值」收敛为「落地实践洞察」——选题主轴回归
「企业AI怎么落地」：落地机制、落地配套（组织/流程/绩效/人才）、落地度量（ROI）、
落地经验与失败原因。泛 AI 技术/产业趋势（合成数据、模型能力、算力、评测）明确降权，
除非直接落到「企业怎么落地」。

反常识度/contrarian 维度已删除：不再计入总分，也不保留为隐性加分项。
权重合计始终为 1.0。

Scoring runs in batch during collect-daily (05:00 system crontab).
Results are cached per URL so the same material isn't re-scored daily.

Batch size: 10 materials per LLM call, configurable below.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

# ── Scoring model ──
# 选题/素材打分使用 DeepSeek API 模型，默认继承 lib.models 的 MODEL_TOPIC。
# SCORING_MODEL 单独设置时覆盖（向后兼容）；否则与选题模型一致。
from lib.models import get_model as _get_model  # noqa: E402
SCORING_MODEL = os.environ.get("SCORING_MODEL", _get_model("topic"))

# ── Batch config ──
MAX_BATCH_SIZE = 10  # materials per LLM call

# ── Cache ──
SCORE_CACHE_FILE = Path(__file__).resolve().parent.parent.parent / "wechat-articles" / "_material_scores.json"

# ── Legacy keyword fallback (kept for backward compatibility for existing materials) ──
ORG_SIGNALS = {
    "across work", "adoption", "agent workflows", "agentic",
    "apps", "budget", "enterprise",
    "harnesses", "incentive", "manager", "models", "organization",
    "permission", "permissions", "procurement",
    "resistance", "role", "roles", "shadow ai", "task",
    "team", "teams", "thresholds", "tools", "trust", "use ai", "workflow",
}

# 企业AI「落地实施」证据 — 本号主轴。真正拉高选题价值的信号：
# 真实工作场景 / 流程改造 / 岗位变化 / 业务结果 / 实施数据 / 可复现步骤。
# 这是评分的第一优先级（高权重）。
ENTERPRISE_IMPLEMENTATION_SIGNALS = {
    # 真实工作场景 / 流程改造
    "workflow redesign", "process redesign", "process change", "workflow change",
    "operating model", "business process", "reengineer", "reengineering",
    "customer service", "support team", "support org", "it ops", "back office",
    "handle time", "ticket", "tickets", "deflect",
    # 岗位变化 / 组织与人才落地
    "role change", "job change", "new role", "reskilling", "upskilling",
    "workforce", "reorganized", "reorg", "new org", "talent", "hiring",
    # 业务结果 / 实施数据
    "roi", "cost savings", "cost reduction", "productivity", "automation rate",
    "accuracy", "revenue", "measured", "metrics", "benchmark", "before and after",
    "percent", "increase", "reduced", "saved hours",
    # 可复现步骤 / 落地方法 / 踩坑
    "case study", "lessons learned", "rolled out", "go live", "deployment",
    "implementation", "piloted", "pilot", "playbook", "how we", "step-by-step",
    "agent", "agents", "copilot", "assistant", "workflow automation",
}

# 纯治理/风险/安全词 — 本号只作为「企业AI落地」的辅助条件，不作为独立高分捷径。
# 命中但缺少落地证据（ENTERPRISE_IMPLEMENTATION_SIGNALS）时不加分，反而压低分。
GOVERNANCE_TERMS = {
    "governance", "internal governance", "risk management", "safeguards",
    "compliance", "regulation", "regulatory", "policy", "policies",
    "alignment", "safety", "responsible", "interpretability", "explainability",
    "risk framework", "guardrails",
}

LOW_FIT_SIGNALS = {
    "feature launch", "package", "paid plan", "plan", "pricing",
    "subscription", "tier", "tiers",
    # 泛增长/营销/广告话题 — 与本号「企业AI落地」定位无关，直接压低分
    "ad campaign", "ad spend", "advertising", "ad visibility", "campaign",
    "growth", "growth hacking", "marketing", "paid acquisition",
    "seo", "conversion rate", "funnel", "brand awareness",
    # 纯创投/融资 — 无落地价值
    "fundraising", "valuation", "seed round", "series a", "series b",
    # 纯基础设施/算力/研究，与企业落地无关（OlmoEarth 类）
    "geo", "satellite", "planetary", "compute", "chip", "semiconductor",
}

# ── Score cache schema version ──
# Bump this whenever SCORING_SYSTEM_PROMPT or the weight formula changes,
# so old cached scores (e.g. v2-topic-relevance) are discarded and materials
# are re-scored with the current rubric.
SCORE_CACHE_VERSION = "v6-landing-practice"


# 执行细节信号：用于案例池（case_pool）筛选。
# 聚焦「企业落地证据」——业务结果、具体工作场景、可复现步骤。
# 纯治理/风险/安全词的命中已被移除（不再作为 case 高分捷径）。
EXECUTION_DETAIL_SIGNALS = {
    # 业务结果 / 实施数据
    "roi", "cost savings", "cost reduction", "productivity", "automation rate",
    "deflection rate", "handle time", "ticket", "tickets", "revenue",
    "measured", "metrics", "accuracy", "percent", "before and after", "saved hours",
    # 真实工作场景 / 流程改造
    "workflow redesign", "process redesign", "process change", "workflow change",
    "customer service", "support team", "support org", "it ops", "back office",
    # 可复现步骤 / 落地过程
    "step-by-step", "step 1", "step 2", "how to", "playbook", "lessons learned",
    "rolled out", "go live", "deployment", "piloted", "pilot", "case study",
    "prompt engineering", "system prompt", "agent", "agents", "copilot",
}


# ── LLM scoring prompt ──

SCORING_SYSTEM_PROMPT = """你是一个内容选题评估专家。你的任务是评估一篇新闻/文章/报告片段的选题价值，判断它是否值得写成一篇深度公众号文章。

**目标读者（决定一切判断标准）：企业 AI 落地的决策者——老板、高管、业务负责人、CIO/CTO。** 他们关心的是"我的企业怎么把 AI 落下去"：该不该投、先投哪个环节、业务流程怎么变、组织/绩效/人才怎么配套、ROI 怎么度量、有什么代价和坑。**他们不关心 AI 行业的技术/产业趋势本身（合成数据、模型能力、算力、评测）——除非它直接落到"企业怎么落地"。** 一篇高段位文章 = 能从具体素材里提炼出对同类企业普遍成立的落地机制与决策依据，让决策者读完能带走一个落地判断。

## 评价维度

### 维度1：主题相关度（Topic Relevance）
权重：30%

判断标准：这条素材与**「企业AI落地实践」核心定位**的关联度有多高？本号只做"企业AI怎么落地"相关深度内容，**不关心AI行业的技术/产业趋势本身——除非它直接落到"企业怎么落地"**。

本号明确定位（必须严格遵循）：
- ✅ **相关**：企业採用AI的實際落地 —— AI Agent在企业生产环境的部署、真实工作场景/业务流程改造、岗位与组织结构变化、业务结果（ROI/降本/提效/自动化率）、实施数据与可复现步骤、企业AI落地的具体案例/方法论/踩坑与经验、落地所需的组织/流程/绩效/人才配套、落地的失败原因与决策取舍
- ✅ **相关**（次之，作为落地辅助条件）：企业AI落地配套的治理/风险/合规（**仅在服务于「让企业AI真正落地并产生业务后果」这一主轴时才有价值**）
- ⚠️ **相关度打折（给 4-6 分，除非直接关联企业落地）**：泛AI技术/产业趋势——模型能力提升、合成数据、算力/芯片、评测基准、AI行业融资、大模型研究进展。**这些话题老板不关心，只有明确写到"企业怎么用/怎么部署/业务怎么变"时才给高分**
- ❌ **不相关**（给 0-2 分）：
  - 泛增长/营销/广告话题（growth、marketing、advertising、campaign、ad visibility、付费投放、SEO获客）
  - 纯个人成长/领导力/产品方法论（与AI企业落地无关时）
  - 纯技术研究/基础设施（论文、芯片、云计算算力、卫星/地理空间AI等，与"企业AI落地"无直接关联）
  - 纯创投/融资新闻（fundraising、估值，无落地价值）
  - 泛科技新闻（与AI企业落地无直接关系）
  - **纯治理/风险/安全/对齐话题**（responsible AI、AI safety、align、policy、regulation 等）：**治理/安全只有在能具体地指出它对业务后果的影响（例如合规缺口如何阻止了某条生产流程上线、安全失败如何造成了可量化的业务成本/事故）时才有价值**；纯谈治理框架/政策/对齐理论而无业务后果的素材，即使来源权威（维度5），主题相关度最多给中低分（≤4），**不得仅凭出现治理/安全关键词就给高分**
- 高分（8-10）：核心就是企业AI落地实践，且对目标读者（企业AI落地决策者）有直接价值
- 中分（5-7）：与企业AI落地相关，但角度偏研究/泛化/技术趋势，或落地价值间接
- 低分（0-4）：与企业AI落地关联弱，或跑偏到技术趋势/推广/营销/纯研究

### 维度2：落地实践洞察（Landing Practice Insight）
权重：35%（最高权重维度）

判断标准：**这条素材能否支撑一篇"企业AI落地实践"主题下的高段位文章？** 高段位 = 能提炼出对同类企业普遍成立的**落地机制与决策依据**，回答"企业怎么把AI落下去"这个问题，而不是"AI行业在发生什么"。

具体看三点：

1. **可迁移的落地机制**：素材是否内含一个对同类企业普遍成立的落地判断——例如"AI项目失败的主因是组织而非技术""落地时要先接数据管道再谈模型""业务流程不重构，提效只会局部化""推理速度是客服AI可用性的生死线""绩效制度不跟着业务流程变，提效会被组织吃掉"这类可复述的落地规律。
2. **对落地决策的含义**：素材能否回答决策者的问题——该不该投、先投哪个环节、组织/流程/绩效/人才怎么配套调整、ROI怎么度量、什么环节该自动化、什么环节必须留人、最常见最贵的坑在哪、失败为什么发生。
3. **信息质量 > 信源等级**：社区讨论/个人博客（Reddit、HN 等）若含一手实施经验、真实踩坑、可核实的细节，本维度可以给高分——**信源等级低不构成洞察价值的天然上限**；反之，大厂报告若只是产品宣传或空泛趋势，本维度不给高分。

**排除**：素材核心是AI技术/产业趋势（合成数据、模型能力、算力、评测、行业融资），没有落到"企业怎么落地"的，本维度最高给 4 分——即使它机制清晰，因为它不是本号的主轴。

- 高分（8-10）：素材有清晰的落地机制/规律 + 明确的落地决策含义，足以支撑一篇面向老板、可被转述和引用的落地实践文章
- 中分（5-7）：有一定落地洞察但偏执行细节，或决策含义间接，需要写作时升维
- 低分（0-4）：纯执行细节/纯技术说明/纯新闻播报/纯AI行业趋势，无落地机制、无决策含义
- **若素材正文信息不足（只有标题/摘要时的占位内容），本维度应保守给分，不要仅凭标题或"AI正在改变企业"这类空泛词给高分**

### 维度3：信息密度（Information Density）
权重：15%

判断标准：这篇文章包含多少硬信息？
- 高分（8-10）：有具体的实验方法、数据表格、案例细节、可验证的事实链条、机制解释
- 中分（5-7）：有一些数据和案例，但不够具体，或者数据本身有点弱
- 低分（0-4）：几乎全是论点、判断、趋势描述，没有具体细节支撑

### 维度4：写作价值（Writing Value）
权重：10%

判断标准：写一篇公众号文章值得吗？
- 高分（8-10）：话题本身能吸引决策者点击，不跟最近热门文章撞车，读者读完可以转述，有传播潜力
- 中分（5-7）：有一定写作价值，但可能需要好角度来包装
- 低分（0-4）：话题太窄、太老、太泛，或者已经被写烂了

特别标记：如果一个话题虽然不是大众向但正好适合你的目标读者（企业AI决策者），可以适当加分。

### 维度5：信源等级（Source Authority）
权重：10%

判断标准：信息来自哪里？
- 10分：大厂一手报告/白皮书（Google、Microsoft、OpenAI、Anthropic、Meta、Amazon、Apple）、顶级咨询公司（McKinsey、BCG、Bain、Deloitte、PwC、Accenture、KPMG）、顶级学术论文（Nature、Science、NeurIPS、ICML）、权威媒体深度调查（NYT、WSJ、FT、Bloomberg、New Yorker）
- 7-9分：行业研究机构（Gartner、Forrester、IDC）、知名投资机构分析（a16z、Sequoia、Index Ventures）、官方技术博客（Google AI Blog、OpenAI Blog）
- 4-6分：可信媒体的分析/观点文章、一般学术论文、知名个人博客
- 1-3分：一般媒体报道、个人博客、社区讨论（Reddit 等）
- 0分：来源不明、纯翻译/搬运、AI摘要生成的文章

**注意**：信源等级只反映"证据的可信度基线"，不代表选题段位。社区讨论的一手实施经验（1-3分）如果机制清晰、细节可核实，维度2 仍可给高分——本号明确允许以高质量社区素材为核心写高段位文章。

## 评分规则

1. 每个维度输出 0-10 分（一位小数）
2. **主题相关度（topic_relevance）为决定性维度**：如果该素材与「企业AI落地」核心定位不相关（属于上面❌列表），主题相关度必须给 0-2 分，总分会被压制到不可能被选中。
3. **决策者洞察（executive_insight）为段位维度**：总分主要由它拉开差距。两条素材同样相关时，能支撑"面向老板的升维文章"的那条必须显著更高。
4. 总分 = 主题相关度×0.30 + 决策者洞察×0.35 + 信息密度×0.15 + 写作价值×0.10 + 信源等级×0.10（权重合计=1.0）
5. 总分也是 0-10 分（一位小数）
6. **正文信息不足时不要假装能完整评分**：如果只提供了标题/摘要、没有足够正文（`needs_content` 场景），应在 `reasoning` 中注明"正文不足/评分受限"，各维度只依据现有信息保守给分，`total` 不得超过 5，绝不要仅凭标题出现的治理/安全/转型/关键词给出虚高总分；正文彻底缺失的素材统一由上级逻辑标记 `insufficient_content` 待补抓，不要代为补足。（若正文是成段、信息充分的真实文章，即使有重复或铺垫，也应正常各维度评分，不因行文啰嗦而清零。）
7. **治理/风险/安全词 ≠ 高分捷径**：治理/安全/合规话题只有在能具体连到业务后果（如阻止上线、可量化成本、事故损失、流程变更）时才加分；通篇只是治理框架/政策/对齐理论而无业务后果，主题相关度最高给中低分（≤4），决策者洞察维度也不得仅凭"AI正在改变治理"这类空泛词给高分。

## 输出格式

只输出 JSON，不要输出其他内容。

```json
{
  "scores": {
    "topic_relevance": 8.5,
    "executive_insight": 8.0,
    "info_density": 7.0,
    "writing_value": 7.5,
    "source_authority": 8.0
  },
  "total": 7.9,
  "reasoning": "简短评价（一句话最多50字，需注明主题相关度判断，并注明是真实正文评分还是占位/不足）"
}
```
"""

def _load_score_cache() -> dict:
    """Load cached LLM scores per URL.

    If the cache was written under an older SCORE_CACHE_VERSION (e.g.
    pre-topic-relevance rubric), discard it so materials get re-scored with
    the current prompt.
    """
    if SCORE_CACHE_FILE.exists():
        try:
            cache = json.loads(SCORE_CACHE_FILE.read_text(encoding="utf-8"))
            if cache.get("_version") != SCORE_CACHE_VERSION:
                print(f"  ↺ Score cache schema mismatch (have '{cache.get('_version')}', want '{SCORE_CACHE_VERSION}'); discarding")
                return {}
            return cache
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_score_cache(cache: dict) -> None:
    """Persist score cache."""
    cache["_version"] = SCORE_CACHE_VERSION
    SCORE_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SCORE_CACHE_FILE.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _batch_call_llm(materials: list[dict]) -> list[dict]:
    """Send a batch of materials to the LLM for scoring.
    
    Returns list of dicts: [{url, total, ...scores, reasoning}]
    One item per input material, in same order.
    """
    if not materials:
        return []

    batch_text = "\n\n---\n\n".join(
        f"素材 {i+1}\n来源URL: {m.get('url', m.get('canonical_url', '?'))}\n标题: {m.get('title', '')}\n摘要: {m.get('summary', '')}\n正文前2000字: {(m.get('content_text', '') or m.get('content', '') or '')[:2000]}"
        for i, m in enumerate(materials)
    )

    user_prompt = f"""请对以下 {len(materials)} 篇素材分别评分。

{batch_text}

对每个素材分别输出评分 JSON，放在一个数组里，按素材顺序排列。

```json
[
  {{{{ 素材1的评分 }}}},
  {{{{ 素材2的评分 }}}}
]
```
"""

    messages = [
        {"role": "system", "content": SCORING_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    # Import LLM client inline to avoid circular import at module level
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from lib.llm import call_model

    try:
        response = call_model(messages, temperature=0.3, max_tokens=4096, model=SCORING_MODEL)
    except Exception as e:
        print(f"  ⚠️ LLM scoring batch failed: {e}")
        return []

    if not response:
        return []

    # Extract JSON array from response
    json_match = re.search(r'```(?:json)?\n(.*?)\n```', response, re.DOTALL)
    if json_match:
        json_str = json_match.group(1)
    else:
        json_str = response.strip()

    try:
        results = json.loads(json_str)
        if not isinstance(results, list):
            results = [results]  # single result wrapped
    except json.JSONDecodeError:
        print(f"  ⚠️ Could not parse LLM scoring response: {response[:200]}")
        return []

    # Map back to input order
    scored = []
    for i, m in enumerate(materials):
        if i < len(results):
            r = results[i]
            entry = {
                "url": m.get("url", m.get("canonical_url", f"unknown_{i}")),
                "total": r.get("total", 0.0),
                "scores": r.get("scores", {}),
                "reasoning": r.get("reasoning", ""),
            }
        else:
            entry = {
                "url": m.get("url", m.get("canonical_url", f"unknown_{i}")),
                "total": 0,
                "scores": {},
                "reasoning": "LLM returned fewer results than input materials",
            }
        scored.append(entry)

    return scored


# ── Public API ──

def editorial_fit_score(material: dict) -> float:
    """LLM-based editorial fit score, with keyword fallback.

    Checks cache first; if no cached LLM score, falls back to keyword scoring.
    Cache is populated during collect-daily batch runs.
    """
    url = material.get("url", material.get("canonical_url", ""))
    if url:
        cache = _load_score_cache()
        cached = cache.get(url)
        if cached is not None:
            total = cached.get("total", 0.0)
            if total > 0:
                # Normalize from 0-10 to 0-1 for downstream compatibility
                return max(0.0, min(1.0, total / 10.0))

    # Fallback: keyword scoring
    text = " ".join(
        [
            material.get("title", ""),
            material.get("summary", ""),
            material.get("content_text", ""),
            material.get("content", ""),
        ]
    ).lower()
    impl_hits = sum(1 for signal in ENTERPRISE_IMPLEMENTATION_SIGNALS if signal in text)
    org_hits = sum(1 for signal in ORG_SIGNALS if signal in text)
    gov_hits = sum(1 for signal in GOVERNANCE_TERMS if signal in text)
    low_fit_hits = sum(1 for signal in LOW_FIT_SIGNALS if signal in text)

    # 企业AI落地实施证据（impl）是本号主轴，权重最高。
    # 组织/管理类信号（org）作为辅助。
    # 纯治理/风险/安全命中（gov）只作为辅助条件，越多且缺落地证据越压制分。
    raw_score = (
        0.2
        + (impl_hits * 0.14)
        + (org_hits * 0.08)
        - (gov_hits * 0.06)
        - (low_fit_hits * 0.18)
    )

    # 只有治理/安全、没有落地证据的内容，封顶 0.5，杜绝「治理词=高分捷径」。
    if gov_hits > 0 and impl_hits == 0:
        raw_score = min(raw_score, 0.5)

    return max(0.0, min(1.0, raw_score))


def execution_detail_score(material: dict) -> float:
    """Keyword-based execution detail score.

    Driven by enterprise-landing evidence: business results, real work
    scenarios, reproducible steps. Pure governance/risk/safety terms were
    removed from EXECUTION_DETAIL_SIGNALS so they no longer count as a case
    high-score shortcut.
    """
    text = " ".join(
        [
            material.get("title", ""),
            material.get("summary", ""),
            material.get("content_text", ""),
        ]
    ).lower()
    detail_hits = sum(1 for signal in EXECUTION_DETAIL_SIGNALS if signal in text)
    gov_hits = sum(1 for signal in GOVERNANCE_TERMS if signal in text)
    impl_hits = sum(1 for signal in ENTERPRISE_IMPLEMENTATION_SIGNALS if signal in text)

    # 落地证据命中即加分；治理词命中但无落地证据则不构成执行细节。
    raw_score = 0.1 + (detail_hits * 0.2)
    if gov_hits > 0 and impl_hits == 0:
        raw_score = min(raw_score, 0.4)

    return max(0.0, min(1.0, raw_score))


def _try_fetch_content(material: dict, min_chars: int = 500) -> str | None:
    """Best-effort body fetch for materials whose content_text is too short.

    Reuses the crawl4ai-based fetch helper from clustering so scoring can
    retry the real article body before giving up. Returns stripped text if it
    meets min_chars, else None. Silent on failure (caller flags insufficiency).
    """
    url = material.get("canonical_url") or material.get("url", "")
    if not url:
        return None
    try:
        from content_platform.curate.clustering import _fetch_content, _strip_html
    except Exception:
        return None
    markdown = _fetch_content(url)
    if not markdown:
        return None
    text = _strip_html(markdown)
    if len(text) < min_chars:
        return None
    return text


def _mark_insufficient(m: dict, reason: str) -> dict:
    """Annotate a material as having insufficient body for a real full score.

    Sets a placeholder (non-inflated) score, marks needs_content/
    insufficient_content, and flags low confidence so downstream never treats
    this as a genuine full editorial score. Does NOT run keyword scoring on
    titles/summaries to fake a high total.
    """
    m["insufficient_content"] = True
    m["needs_content"] = True
    m["content_status"] = reason  # e.g. "no_body_chars" | "fetch_failed" | "fetched_too_short"
    m["score_confidence"] = "low"
    # Placeholder, intentionally low so it cannot rank as a real pick.
    m["editorial_fit_score"] = 0.0
    m["llm_score_detail"] = {}
    m["llm_score_reasoning"] = f"正文不足，未做真实评分（{reason}）；已标记 insufficient_content"
    return m


def score_materials_batch(materials: list[dict]) -> list[dict]:
    """Score a batch of materials via LLM, cache results, return enriched materials.

    Called by run_collect_daily after curating materials.
    Each returned material has editorial_fit_score and llm_score_detail added.

    Insufficient-content handling (no fake scoring):
      • If a material has too little body (<MIN_CONTENT_CHARS), we first try to
        fetch its real body once via _try_fetch_content. If the body is then
        available it is injected and scored normally.
      • If body still cannot be obtained, the material is NOT scored by title/
        summary keywords to fake a high total. It is flagged insufficient_content
        (needs_content=True) with a low placeholder score so it cannot win a
        pick, and is queued (flagged) for re-fetch on a later run.
    """
    MIN_CONTENT_CHARS = 300

    if not materials:
        return materials

    # Filter to only materials with enough content (attempt one fetch each)
    scorable = []
    no_content_urls: list = []
    for m in materials:
        content = (m.get("content_text", "") or m.get("content", "") or "")
        if len(content) < MIN_CONTENT_CHARS:
            fetched = _try_fetch_content(m, MIN_CONTENT_CHARS)
            if fetched:
                m["content_text"] = fetched
                m["content"] = fetched
                scorable.append(m)
            else:
                no_content_urls.append((m, "no_body_chars"))
        else:
            scorable.append(m)

    if no_content_urls:
        print(
            f"  ⏭️  {len(no_content_urls)} materials without sufficient body "
            f"({'/'.join(sorted({st for _, st in no_content_urls}))}); marking insufficient_content (not keyword-faked)"
        )

    if not scorable:
        print("  ⚠️ No materials have enough content for LLM scoring (all flagged insufficient_content)")
        for m, reason in no_content_urls:
            _mark_insufficient(m, reason)
        return materials

    # Load existing cache
    cache = _load_score_cache()

    # Batch into groups
    new_scores = {}
    for batch_start in range(0, len(scorable), MAX_BATCH_SIZE):
        batch = scorable[batch_start:batch_start + MAX_BATCH_SIZE]

        # Check which URLs are already cached
        uncached = []
        for m in batch:
            url = m.get("url", m.get("canonical_url", ""))
            if url and url in cache and cache[url].get("total", 0) > 0:
                continue
            uncached.append(m)

        if not uncached:
            print(f"  ✅ Batch {batch_start//MAX_BATCH_SIZE + 1}: all cached")
            continue

        print(f"  🧠 Scoring batch {batch_start//MAX_BATCH_SIZE + 1}: {len(uncached)} materials via LLM ({SCORING_MODEL})...")
        results = _batch_call_llm(uncached)
        if not results:
            print("  ⚠️ LLM returned no results for batch; marking insufficient_content instead of keyword-faking")
            for m in uncached:
                _mark_insufficient(m, "llm_empty")
            continue

        for r in results:
            url = r.get("url", "")
            if url:
                cache[url] = r
                new_scores[url] = r

        print(f"    → scored {len(results)} materials")

        # Rate limit: 1s between batches
        if batch_start + MAX_BATCH_SIZE < len(scorable):
            time.sleep(1)

    # Persist cache
    if new_scores:
        _save_score_cache(cache)
        print(f"  💾 Cached {len(new_scores)} new scores")

    # Enrich each material with its score
    for m in materials:
        url = m.get("url", m.get("canonical_url", ""))
        cached = cache.get(url)
        if cached:
            total = cached.get("total", 0.0)
            # Normalize from 0-10 to 0-1
            m["editorial_fit_score"] = max(0.0, min(1.0, total / 10.0))
            m["llm_score_detail"] = cached.get("scores", {})
            m["llm_score_reasoning"] = cached.get("reasoning", "")
            # Real score, high confidence, not insufficient
            m.pop("insufficient_content", None)
            m.pop("needs_content", None)
            m["score_confidence"] = "high"
        else:
            # URL not scored and not present in materials: if it was low-content
            # it was already flagged above; only keyword-score genuine bodies.
            content = (m.get("content_text", "") or m.get("content", "") or "")
            if len(content) >= MIN_CONTENT_CHARS:
                m["editorial_fit_score"] = editorial_fit_score(m)
                m["llm_score_detail"] = {}
                m["score_confidence"] = "medium"
            else:
                _mark_insufficient(m, "no_body_chars")

    # Apply insufficiency flags to the low-content materials at the top level too.
    for m, reason in no_content_urls:
        _mark_insufficient(m, reason)

    return materials
