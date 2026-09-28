from content_platform.curate.scoring import editorial_fit_score
from content_platform.curate.scoring import execution_detail_score


def test_editorial_fit_score_rejects_pricing_without_org_signal():
    material = {
        "title": "GitHub changes Copilot pricing tiers",
        "summary": "new package pricing for teams",
        "content_text": "pricing package subscription tier changes for paid plans",
        "tags": {"topic_focus": ["product strategy"]},
    }

    score = editorial_fit_score(material)

    assert score < 0.5


def test_editorial_fit_score_rewards_org_change_signal():
    material = {
        "title": "Why enterprise AI adoption fails in support teams",
        "summary": "workflow redesign, manager incentives and trust",
        "content_text": (
            "enterprise workflow redesign changed team roles, manager incentives, "
            "support team deflected tickets, adoption resistance and permissions"
        ),
        "tags": {"topic_focus": ["adoption"]},
    }

    score = editorial_fit_score(material)

    assert score >= 0.75


def test_editorial_fit_score_rewards_agentic_workflow_guidance():
    material = {
        "title": "A Guide to Which AI to Use in the Agentic Era",
        "summary": "It's not just chatbots anymore",
        "content_text": (
            "using AI has changed dramatically as agent workflows become practical. "
            "you can assign them to a task and they do them, using tools as appropriate. "
            "because of this change, you have to consider models, apps, and harnesses "
            "when deciding what AI to use across work."
        ),
        "tags": {"topic_focus": ["agentic ai"]},
    }

    score = editorial_fit_score(material)

    assert score >= 0.65


def test_editorial_fit_score_rewards_measurable_implementation_case():
    material = {
        "title": "How a support org deployed an AI agent to deflected tickets",
        "summary": "measured cost savings and automation rate after go live",
        "content_text": (
            "after deployment, the agent deflected 38% of tickets, handle time dropped, "
            "the team reported measurable ROI and cost savings in the first quarter."
        ),
        "tags": {"topic_focus": ["case-study"]},
    }

    score = editorial_fit_score(material)

    assert score >= 0.75


def test_editorial_fit_score_does_not_reward_governance_alone():
    # 纯治理/风险/安全/对齐话题，缺少企业落地证据 → 不应成为高分捷径。
    material = {
        "title": "Responsible Scaling Policy",
        "summary": "updated risk governance framework for frontier AI systems",
        "content_text": (
            "this policy introduces capability thresholds, internal governance, "
            "risk management, safeguards, and model evaluation processes."
        ),
        "tags": {"topic_focus": ["governance"]},
    }

    score = editorial_fit_score(material)

    assert score < 0.6


def test_execution_detail_does_not_reward_governance_alone():
    # 纯治理性语言命中（framework/eval/process 等）不足以让案例池高分通过。
    material = {
        "title": "Responsible Scaling Policy",
        "summary": "updated risk governance framework for frontier AI systems",
        "content_text": (
            "this update introduces capability thresholds, internal governance, "
            "risk management, safeguards, model evaluation processes, and "
            "practical implementation guidance."
        ),
    }

    score = execution_detail_score(material)

    assert score < 0.6


def test_execution_detail_rewards_real_implementation_metrics():
    material = {
        "title": "Case study: AI agent in customer service",
        "summary": "deployment with measured metrics",
        "content_text": (
            "after the pilot went live, automation rate reached 42%, cost savings "
            "were measured and handle time dropped by a third."
        ),
    }

    score = execution_detail_score(material)

    assert score >= 0.6


# ── Rubric structure: contrarian removed, transformation added, weights = 1.0 ──

from content_platform.curate.scoring import (
    SCORING_SYSTEM_PROMPT,
    SCORE_CACHE_VERSION,
)


def test_rubric_drops_counter_intuitive_dimension():
    # 反常识度/contrarian 维度不得再出现在评分提示词或输出字段中（也不作隐性加分）。
    assert "反常识度" not in SCORING_SYSTEM_PROMPT
    assert "Counter-intuitiveness" not in SCORING_SYSTEM_PROMPT
    assert "counter_intuitive" not in SCORING_SYSTEM_PROMPT


def test_rubric_uses_landing_practice_insight_dimension():
    # v6：维度2 从「企业转型洞察」收敛为「落地实践洞察」，对齐「企业AI怎么落地」主轴。
    assert "落地实践洞察" in SCORING_SYSTEM_PROMPT
    assert "Landing Practice Insight" in SCORING_SYSTEM_PROMPT


def test_total_weights_sum_to_one():
    # 权重合计必须为 1.0：0.35 + 0.25 + 0.15 + 0.15 + 0.10 = 1.00
    import re
    weights = sum(float(x) for x in re.findall(r"×([0-9]\.[0-9]{2})", SCORING_SYSTEM_PROMPT))
    assert abs(weights - 1.0) < 1e-9, weights


def test_govsafety_must_tie_to_business_consequence():
    # 评分提示词须明确：治理/安全只有连到业务后果才有价值，不能凭关键词高分。
    assert "治理/安全只有在能具体地指出它对业务后果的影响" in SCORING_SYSTEM_PROMPT
    assert "不得仅凭出现治理/安全关键词就给高分" in SCORING_SYSTEM_PROMPT


def test_cache_version_bumped_for_new_rubric():
    assert SCORE_CACHE_VERSION == "v6-landing-practice"


# ── Insufficient-content handling: never fake score from titles ──

from content_platform.curate.scoring import score_materials_batch


def test_score_batch_flags_insufficient_content_not_fake_score():
    # 正文过短的素材不得被标题关键词假装完整评分：
    # 应被标记 insufficient_content / needs_content，置信度低，editorial_fit_score 为占位低值。
    material = {
        "url": "https://unknown.invalid/only-title",
        "canonical_url": "https://unknown.invalid/only-title",
        "title": "Responsible AI safety governance policy alignment framework",
        "summary": "new governance framework for responsible AI",
        "content_text": "short",
    }
    result = score_materials_batch([dict(material)])[0]

    assert result.get("insufficient_content") is True
    assert result.get("needs_content") is True
    assert result.get("score_confidence") == "low"
    # 占位低值，绝不能因标题命中治理/转型关键词而虚高
    assert result.get("editorial_fit_score", 1.0) == 0.0


def test_score_batch_does_not_keyword_fake_low_content():
    # 即使标题/摘要充满治理/落地关键词，正文不足也必须标 insuffiient，而不是 keyword 高分。
    material = {
        "url": "https://unknown.invalid/keyword-title",
        "canonical_url": "https://unknown.invalid/keyword-title",
        "title": "Enterprise AI: workflow redesign, agents transform customer service ROI",
        "summary": "AI agents, workflow redesign, cost savings, measured ROI",
        "content_text": "",  # 无正文
    }
    result = score_materials_batch([dict(material)])[0]

    assert result.get("insufficient_content") is True
    assert result.get("editorial_fit_score", 1.0) == 0.0
