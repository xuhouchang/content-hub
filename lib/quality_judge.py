#!/usr/bin/env python3
"""公众号内容质量：LLM 语义评估闸门（作为规则硬门槛之上的第二道闸）。

设计目标
--------
在既有的纯规则质量闸门（write_article.check_article_quality）之上，增加一道
「大模型语义评估」，避免只靠关键词/正则把「看似合格、实则空转或堆砌」的内容放行。

关键约束（fail-safe，绝不伪造通过）
--------------------------------
1. 规则硬门槛通过 之后，才进入 LLM 语义评估（LLM 只做增强，不做放行盗用）。
2. LLM 调用失败 / 返回不可解析 / 明确给出 FAIL -> 一律置为「不可通过」；
   明确不可用（unavailable，例如凭证缺失、网络失败、返回空）-> 绝不伪造通过，
   置为显式 unavailable，并把判定交还给调用方（默认视为不可通过）。
3. 纯函数，复用 lib.llm.call_model（DeepSeek 直连优先），不新增网络栈。

评估维度（每个维度给分 1-5 / 理由 / 改进建议）
---------------------------------------------
  thesis, mechanism, evidence, source_traceability, counterpoint,
  independent_judgment, material_dump, style

输出结构（严格 JSON，容忍 code-fence）
--------------------------------------
{
  "verdict": "pass" | "fail" | "unavailable",
  "overall_score": 0-5,
  "overall_reason": "...",
  "dimensions": {
     "<dim>": {"score": 1-5, "reason": "...", "suggestion": "..."}
  },
  "must_fix": ["..."]   // 必须修复项（verdict=fail 时非空）
}
"""

from __future__ import annotations

import json
import re
from typing import Optional

from lib.llm import call_model

# 评估维度（语义层，它们是对规则层 check_article_quality 8 项检查的语义复核/补强）
DIMENSIONS = [
    "thesis",               # 是否有可复述、可验证的核心判断（而非空话/罗列）
    "mechanism",            # 是否讲清了因果/传导机制（而非只贴结论）
    "evidence",             # 证据是否具体、可核（数字/出处/案例）而非断言
    "source_traceability",  # 每个主张是否可追溯到素材来源
    "counterpoint",         # 是否处理反方观点或明确边界
    "independent_judgment", # 是否有独立判断，而非素材复述/缝合
    "material_dump",        # 反向维度：是否素材堆砌（分高=越不像堆砌）
    "style",                # 是否面向公众号读者、结构清晰、无硬伤
]

JUDGE_SYSTEM_PROMPT = """你是一名严谨的资深公众号内容主编与事实核查专家。
请对给出的文章草稿做严格的语义质量评估。评估必须基于文本本身，不得放水。

你只能输出严格 JSON（不要输出 JSON 之外的任何文字、解释、代码块围栏外的内容）。
JSON 结构必须严格如下：

{
  "verdict": "pass" 或 "fail" 或 "unavailable"(仅当信息不足以判断，尽量别用),
  "overall_score": 0 到 5 的整数,
  "overall_reason": "一句话总评",
  "dimensions": {
    "thesis": {"score": 1到5, "reason": "简短理由", "suggestion": "具体可落地的改进建议"},
    "mechanism": {"score": 1到5, "reason": "", "suggestion": ""},
    "evidence": {"score": 1到5, "reason": "", "suggestion": ""},
    "source_traceability": {"score": 1到5, "reason": "", "suggestion": ""},
    "counterpoint": {"score": 1到5, "reason": "", "suggestion": ""},
    "independent_judgment": {"score": 1到5, "reason": "", "suggestion": ""},
    "material_dump": {"score": 1到5, "reason": "", "suggestion": ""},
    "style": {"score": 1到5, "reason": "", "suggestion": ""}
  },
  "must_fix": ["必须修复项1", "必须修复项2"]
}

评分口径：
- material_dump 为“反向/正向混合”维度：分数越高代表越不像素材堆砌（即独立重写越充分）。
  若文章大段直接复述素材/无作者加工，此维度应给低分（1-2）。
- 全局 verdict = fail 的典型情形：整体空洞、机制缺失、素材拼贴、主张无法追溯、存在明显事实硬伤。
- 只要任一维度存在阻断性硬伤，verdict 应判 fail。

请只输出上述 JSON 对象本身。"""


def build_judge_prompt(article_text: str, title: str = "", blueprint_text: str = "") -> list:
    """构造 LLM judge 的 messages（system + user）。"""
    user = "请评估下面这篇公众号文章草稿。\n"
    if title:
        user += f"\n## 标题\n{title}\n"
    if blueprint_text:
        # 提供蓝图（thesis/机制链/证据/边界/来源映射）便于核对 traceability 与独立判断
        user += f"\n## 创作蓝图（供核对主张是否可追溯）\n{blueprint_text}\n"
    user += f"\n## 文章草稿\n{article_text}\n"
    user += ("\n请严格按要求输出 JSON（只输出 JSON 对象，不要代码围栏）。")
    return [
        {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def parse_judge_json(raw: Optional[str]) -> Optional[dict]:
    """从 LLM 响应中容错提取 judge JSON，容忍代码围栏/前后杂文。失败返回 None。"""
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    # 去掉 markdown code fence
    fence = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    else:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start:end + 1]
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        return None
    return None


def _default_unavailable(reason: str) -> dict:
    """构造显式的「不可用」判定 —— 绝不伪造通过。"""
    return {
        "verdict": "unavailable",
        "overall_score": 0,
        "overall_reason": reason,
        "dimensions": {},
        "must_fix": [reason],
    }


def parse_judge_verdict(obj: dict) -> dict:
    """规范化 judge 返回：补齐默认维度、强制合法化 verdict/分数。任何非法 -> unavailable。"""
    if not isinstance(obj, dict):
        return _default_unavailable("LLM judge 返回非 JSON 对象")
    verdict = obj.get("verdict")
    if verdict not in ("pass", "fail", "unavailable"):
        return _default_unavailable(f"LLM judge verdict 非法: {verdict!r}")
    dims = obj.get("dimensions")
    if not isinstance(dims, dict):
        dims = {}
    normalized = {}
    for d in DIMENSIONS:
        entry = dims.get(d)
        if not isinstance(entry, dict):
            entry = {}
        try:
            score = max(1, min(5, int(entry.get("score", 0))))
        except (TypeError, ValueError):
            score = 0  # 非法分数 -> 视为缺失
        normalized[d] = {
            "score": score,
            "reason": str(entry.get("reason", "") or ""),
            "suggestion": str(entry.get("suggestion", "") or ""),
        }
    try:
        overall = max(0, min(5, int(obj.get("overall_score", 0))))
    except (TypeError, ValueError):
        overall = 0
    must_fix = obj.get("must_fix")
    must_fix = [str(m) for m in must_fix] if isinstance(must_fix, list) else []
    return {
        "verdict": verdict,
        "overall_score": overall,
        "overall_reason": str(obj.get("overall_reason", "") or ""),
        "dimensions": normalized,
        "must_fix": must_fix,
    }


def run_llm_judge(
    article_text: str,
    title: str = "",
    blueprint_text: str = "",
    model: Optional[str] = None,
    max_tokens: int = 1500,
    temperature: float = 0.0,
) -> dict:
    """调用 LLM（DeepSeek 直连，经 lib.llm.call_model）做语义评估。

    fail-safe：任何失败/非法/空响应 -> 返回 verdict="unavailable"（绝不伪造通过）。
    返回规范化后的 judge 判定 dict。
    """
    messages = build_judge_prompt(article_text, title=title, blueprint_text=blueprint_text)
    try:
        raw = call_model(messages, temperature=temperature, max_tokens=max_tokens, model=model)
    except Exception as e:  # 绝不因 LLM 抛错而伪造通过
        return _default_unavailable(f"LLM judge 调用失败: {e}")
    parsed = parse_judge_json(raw)
    if parsed is None:
        return _default_unavailable("LLM judge 返回无法解析（空/错误格式），无法评估")
    return parse_judge_verdict(parsed)


def evaluate_quality(
    article_text: str,
    title: str = "",
    source_map: Optional[list] = None,
    blueprint_text: str = "",
    rule_gate=None,
    model: Optional[str] = None,
    json_only: bool = True,
) -> dict:
    """完整质量闸门 = 规则硬门槛（先）+ LLM 语义评估（增强）。

    返回 dict（兼容现有 check_article_quality 的字段，另加语义层）：
      passed          : bool  最终是否通过（LLM unavailable 时按不可通过处理）
      status          : "rules"|"rules+llm"|"unavailable"
      rules           : 规则层结果（原 check_article_quality 输出）
      llm             : LLM judge 输出（含 verdict/overall_score/dimensions/must_fix）
      score, checks, warnings, reasons : 兼容字段
    """
    if rule_gate is None:
        from write_article import check_article_quality
        rule_gate = lambda t, **kw: check_article_quality(t, title=kw.get("title", title), source_map=kw.get("source_map", source_map))
    rules = rule_gate(article_text, title=title, source_map=source_map) if callable(rule_gate) else {}

    reasons = list(rules.get("reasons", []))
    warnings = list(rules.get("warnings", []))

    # ── 规则硬门槛先决：rules 不过 => 直接不可通过，无需浪费 LLM ──
    if not rules.get("passed", False):
        return {
            "passed": False,
            "status": "rules",
            "rules": rules,
            "llm": None,
            "score": rules.get("score", 0),
            "checks": rules.get("checks", {}),
            "warnings": warnings,
            "reasons": reasons + ["规则硬门槛未通过，LLM 语义评估未执行"],
        }

    # ── 规则过 => 进入 LLM 语义评估 ──
    llm = run_llm_judge(article_text, title=title, blueprint_text=blueprint_text, model=model)
    verdict = llm.get("verdict")

    if verdict == "unavailable":
        # 明确不可用：绝不伪造通过。置为不可通过，但附上可辨识的可用性标记。
        reasons.append("LLM 语义评估不可用（无凭证/调用失败/响应非法），按不可通过处理")
        warnings.append("quality gate 处于 degraded 模式：仅规则层生效，无大模型语义复核")
        return {
            "passed": False,
            "status": "unavailable",
            "rules": rules,
            "llm": llm,
            "score": rules.get("score", 0),
            "checks": rules.get("checks", {}),
            "warnings": warnings,
            "reasons": reasons,
        }

    if verdict == "fail":
        reasons.append(f"LLM 语义评估未通过: {llm.get('overall_reason', '')}")
        reasons.extend(llm.get("must_fix", []))
        return {
            "passed": False,
            "status": "rules+llm",
            "rules": rules,
            "llm": llm,
            "score": rules.get("score", 0),
            "checks": rules.get("checks", {}),
            "warnings": warnings,
            "reasons": reasons,
        }

    # verdict == "pass"
    warnings.append("LLM 语义评估通过（规则硬门槛 + 语义增强双过）")
    return {
        "passed": True,
        "status": "rules+llm",
        "rules": rules,
        "llm": llm,
        "score": rules.get("score", 0),
        "checks": rules.get("checks", {}),
        "warnings": warnings,
        "reasons": reasons,
    }


if __name__ == "__main__":
    # 快速自检
    sample = "# 示例\n\n" + "组织跟不上才是瓶颈，因为数据分散导致判断滞后，所以试点无法复制。" * 3
    print(json.dumps(build_judge_prompt(sample, title="测试"), ensure_ascii=False, indent=2)[:500])
