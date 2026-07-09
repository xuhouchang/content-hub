"""
飞书文档日志接口。

管理"量化 ETF 运行日志"文档的增补写入。
每次 append 一个完整日期块：今日预测 + 历史复盘 + 统计摘要。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any

from config.settings import get_settings
from config.universe import Universe
from .feishu_client import append_to_doc

logger = logging.getLogger(__name__)

HKT = timezone(timedelta(hours=8))

# 量化 ETF 运行日志 固定文档 ID（user 创建，确保 lark-cli user 有写权限）
FEISHU_DOC_TOKEN = "X6wLdbf4yoYpiOx4ncWcbRaKnnd"


def log_daily(
    rotation_result: Any,
    position_result: Any,
    report_date: str | None = None,
    evaluations: list | None = None,
    signal_logs: list[dict] | None = None,
    close_prices: dict[str, Any] | None = None,
) -> None:
    """将今日预测 + 历史复盘合并写入飞书文档（一次 append）。

    文档结构：
      ## YYYY-MM-DD
      **今日预测**
      - ...
      **信号明细**
      - ...
      **复盘 (YYYY-MM-DD 预测)**
      - 判定：✅ 正确 / ❌ 错误
      - 组合收益：...
      - ...
      **[近 N 条统计]**
    """
    if report_date is None:
        settings = get_settings()
        report_date = datetime.now(HKT).strftime(settings.report.date_format)

    lines = _build_prediction_block(rotation_result, position_result, report_date)

    # 追加历史复盘
    if evaluations and signal_logs:
        review_lines = _build_review_block(evaluations, signal_logs, close_prices or {})
        lines.extend(review_lines)

    content = "\n".join(lines)

    try:
        append_to_doc(FEISHU_DOC_TOKEN, content)
        logger.info("运行日志已写入飞书文档: %s", report_date)
    except Exception as e:
        logger.warning("写入飞书文档失败: %s", e)


def _build_prediction_block(
    rotation_result: Any,
    position_result: Any,
    report_date: str,
) -> list[str]:
    """构建今日预测段落。"""
    lines = [
        "",
        f"## {report_date}",
        "",
        "**今日预测**",
        "",
    ]

    if rotation_result.is_defensive:
        lines.append("- 建议：全仓现金（防守）")
    else:
        for t in rotation_result.selected_tickers:
            name = Universe.display_name(t)
            w = position_result.allocations.get(t, 0) * 100
            lines.append(f"- {name} ({t})：{w:.0f}%")

    if position_result.cash_weight > 0:
        lines.append(f"- 现金：{position_result.cash_weight*100:.0f}%")

    lines.append(f"- 理由：{rotation_result.reason}")
    lines.append(f"- 风险等级：{_risk_label(rotation_result)}")
    lines.append("")

    # 信号明细
    if rotation_result.all_ranked is not None and not rotation_result.all_ranked.empty:
        lines.append("**信号明细**")
        lines.append("")
        ranked = rotation_result.all_ranked
        for ticker, row in ranked.iterrows():
            name = Universe.display_name(ticker)
            ret_20 = row.get("ret_20", 0)
            ret_60 = row.get("ret_60", 0)
            hv = f"hv_{get_settings().volatility.window}d"
            hv_val = row.get(hv, 0)
            score = row.get("composite_score", 0)
            lines.append(
                f"- {name} ({ticker})："
                f"ret_20 {ret_20*100:+.1f}% | "
                f"ret_60 {ret_60*100:+.1f}% | "
                f"vol {hv_val*100:.1f}% | "
                f"score {score:.4f}"
            )
        lines.append("")

    return lines


def _build_review_block(
    evaluations: list,
    signals: list[dict],
    close_prices: dict[str, Any],
) -> list[str]:
    """构建历史复盘段落。

    顺序：最近的 T+1 评估 → 多条 T+1 评估 → 滚动统计。
    """
    from review.evaluator import compute_summary
    from review.attribution import attribute

    lines: list[str] = []
    recent_h1 = [e for e in evaluations if e.horizon == 1]

    if not recent_h1:
        return lines

    # ── 最近 1 条 T+1 详细复盘 ───────────────────
    last_ev = recent_h1[0]
    sig = next(
        (s for s in signals if s.get("signal_date") == last_ev.signal_date),
        None,
    )

    verdict = "✅ **正确**" if last_ev.is_outperform else "❌ **错误**"
    direction = "跑赢" if last_ev.is_outperform else "跑输"

    lines.append(f"**复盘 ({last_ev.signal_date})**")
    lines.append("")
    lines.append(f"- 判定：{verdict}")
    lines.append(f"- 预测区间：{last_ev.signal_date} (T+{last_ev.horizon_trading_days}交易日)")
    lines.append(f"- 组合收益：{last_ev.portfolio_return*100:+.2f}%")
    lines.append(f"- 基准收益：{last_ev.benchmark_return*100:+.2f}%")
    lines.append(f"- 超额收益：{last_ev.excess_return*100:+.2f}% ({direction})")

    if sig:
        # 展示当时预测了什么
        rec_weights = sig.get("recommended_weights", {})
        if rec_weights:
            from config.universe import Universe
            rec_str = " / ".join(
                f"{Universe.display_name(t) if t != 'CASH' else '现金'} {w*100:.0f}%"
                for t, w in rec_weights.items() if w > 0
            )
            lines.append(f"- 当时建议：{rec_str}")

    if sig and not last_ev.is_outperform:
        reasons = attribute(sig, last_ev, close_prices)
        if reasons:
            lines.append(f"- 归因：{'; '.join(reasons)}")

    lines.append("")

    # ── 最近 3/5 条 T+1 速览 ────────────────────
    for display_label, n in [("3", 3), ("5", 5)]:
        batch = recent_h1[:n]
        if len(batch) < n:
            continue

        correct = sum(1 for e in batch if e.is_outperform)
        avg_excess = sum(e.excess_return for e in batch) / len(batch)
        lines.append(
            f"- 近 {display_label} 条 T+1：{correct}/{len(batch)} 正确"
            f" | 平均超额 {avg_excess*100:+.2f}%"
        )
    lines.append("")

    # ── 滚动统计 ──────────────────────────────────
    for h in [1, 3, 5]:
        summary = compute_summary(evaluations, horizon=h, n_recent=20)
        if summary.signal_count > 0:
            lines.append(
                f"- 近 {summary.signal_count} 条 T+{h}："
                f"胜率 {summary.outperform_rate:.0f}%"
                f" | 超额 {summary.avg_excess_return*100:+.2f}%"
            )

    lines.append("")

    return lines


def _risk_label(rotation_result: Any) -> str:
    from review.signal_logger import _risk_level
    return _risk_level(rotation_result)
