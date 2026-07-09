"""
自动复盘报告生成。

输出 Markdown 报告到 reports/output/review_YYYY-MM-DD.md。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

from config.settings import get_settings
from config.universe import Universe
from .signal_logger import load_signals
from .evaluator import evaluate_all, compute_summary
from .attribution import attribute

logger = logging.getLogger(__name__)

HKT = timezone(timedelta(hours=8))


def generate_review_report(
    close_prices: dict[str, pd.Series],
    report_date: str | None = None,
    output_dir: str | None = None,
) -> str:
    """生成复盘报告并写入文件。

    Args:
        close_prices: 全量价格数据
        report_date: 日期 YYYY-MM-DD
        output_dir: 输出目录

    Returns:
        报告文件路径
    """
    settings = get_settings()

    if report_date is None:
        report_date = datetime.now(HKT).strftime(settings.report.date_format)

    # 获取历史信号
    signals = load_signals(limit=50)  # 最近 50 条足够复盘

    if not signals:
        logger.info("暂无历史信号，跳过复盘")
        return ""

    # 评估
    horizons = [1, 3, 5, 7]
    evaluations = evaluate_all(signals, close_prices, horizons=horizons)

    if not evaluations:
        logger.info("尚无足够数据评估，跳过复盘")
        return ""

    md = _build_markdown(
        signals, evaluations, close_prices, report_date, horizons, settings
    )

    # 写入
    out_dir = Path(output_dir or settings.report.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    filepath = out_dir / f"review_{report_date}.md"

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(md)

    logger.info("复盘报告已写入: %s", filepath)
    return str(filepath)


def _build_markdown(
    signals: list[dict],
    evaluations: list,
    close_prices: dict[str, pd.Series],
    report_date: str,
    horizons: list[int],
    settings,
) -> str:
    """构建 Markdown 内容。"""
    lines: list[str] = [
        f"# 港股 ETF 复盘报告 — {report_date}",
        "",
    ]

    # ── 最近 N 个信号在各 Horizon 表现（核心复盘表） ─────
    recent_evals = sorted(
        {ev.signal_date: ev for ev in evaluations if ev.horizon == 1}.values(),
        key=lambda e: e.signal_date, reverse=True
    )[:5]
    # 对最近 5 个信号，展示 T+1 / T+7 两个关键 horizon
    for display_label, recent_count in [("5", 5), ("3", 3), ("1", 1)]:
        display_signals = recent_evals[:recent_count]
        display_dates = [e.signal_date for e in display_signals]
        if not display_dates:
            continue

        # 取这些信号在所有 horizon 的数据
        display_evals = [
            e for e in evaluations
            if e.signal_date in display_dates
        ]
        # 只展示 T+1 和 T+7
        show_horizons = [h for h in horizons if h in (1, 7)] or [horizons[0]]

        lines.append(f"## 最近 {display_label} 个信号——各周期表现")
        lines.append("")

        for h in show_horizons:
            h_display = {
                1: "T+1（次日）", 3: "T+3（3日）", 5: "T+5（周内）", 7: "T+7（一周）",
            }.get(h, f"T+{h}")

            h_rows = sorted(
                [e for e in display_evals if e.horizon == h],
                key=lambda e: e.signal_date, reverse=True
            )
            if not h_rows:
                continue

            lines.append(f"### {h_display}")
            lines.append("")
            lines.append("| 日期 | 建议 | 组合收益 | 基准收益 | 超额 | 判定 |")
            lines.append("|------|------|---------|---------|------|------|")
            for ev in h_rows:
                sig = next(
                    (s for s in signals if s.get("signal_date") == ev.signal_date),
                    None,
                )
                if sig:
                    weights_str = " / ".join(
                        f"{Universe.display_name(t) if t != 'CASH' else '现金'} {w*100:.0f}%"
                        for t, w in sig.get("recommended_weights", {}).items()
                        if w > 0
                    )
                else:
                    weights_str = "-"

                lines.append(
                    f"| {ev.signal_date} | {weights_str} "
                    f"| {ev.portfolio_return*100:+.2f}% "
                    f"| {ev.benchmark_return*100:+.2f}% "
                    f"| {ev.excess_return*100:+.2f}% "
                    f"| {'✅ 跑赢' if ev.is_outperform else '❌ 跑输'} |"
                )
            lines.append("")

    # ── 各 horizon 汇总 ──────────────────────────────────
    lines.append("## 各 Horizon 表现汇总")
    lines.append("")
    lines.append("| Horizon | 信号数 | 胜率(跑赢) | 平均超额 | 绝对正收益 |")
    lines.append("|---------|--------|-----------|---------|-----------|")
    for h in horizons:
        h_evals = [e for e in evaluations if e.horizon == h]
        if h_evals:
            n = len(h_evals)
            n_out = sum(1 for e in h_evals if e.is_outperform)
            n_abs = sum(1 for e in h_evals if e.is_absolute_positive)
            avg_ex = sum(e.excess_return for e in h_evals) / n
            lines.append(
                f"| T+{h} | {n} | {n_out/n*100:.1f}% "
                f"| {avg_ex*100:+.2f}% "
                f"| {n_abs/n*100:.1f}% |"
            )
    lines.append("")

    # ── 近 20 条信号统计 ────────────────────────────────
    lines.append("## 近 20 条信号统计")
    lines.append("")
    for h in horizons:
        summary = compute_summary(evaluations, horizon=h, n_recent=20)
        defensive_count = sum(
            1 for sig in signals[:20]
            if sig.get("is_defensive", False)
        )
        if summary.signal_count > 0:
            lines.append(f"**T+{h}:**")
            lines.append(f"- 信号数: {summary.signal_count}")
            lines.append(f"- 跑赢基准: {summary.outperform_rate:.1f}%")
            lines.append(f"- 平均超额: {summary.avg_excess_return*100:+.2f}%")
            lines.append(f"- 绝对正收益: {summary.absolute_positive_rate:.1f}%")
            lines.append(f"- 防守触发: {defensive_count}/{summary.signal_count} "
                        f"({defensive_count/summary.signal_count*100:.1f}%)")
            lines.append("")

    # ── 最近归因分析 ────────────────────────────────────
    failed = [
        e for e in evaluations
        if e.horizon == 1 and not e.is_outperform
    ][:5]  # 最近 5 条失效信号

    if failed:
        lines.append("## 最近失效信号归因")
        lines.append("")
        lines.append("| 日期 | 选择的 ETF | 失效原因 |")
        lines.append("|------|-----------|----------|")
        for ev in failed:
            sig = next(
                (s for s in signals if s.get("signal_date") == ev.signal_date),
                None,
            )
            if sig:
                reasons = attribute(sig, ev, close_prices)
                selected = ", ".join(sig.get("selected_etf", [])) or "全现金"
                reasons_text = "; ".join(reasons)
                lines.append(f"| {ev.signal_date} | {selected} | {reasons_text} |")
        lines.append("")

    # ── 需要关注 ────────────────────────────────────────
    lines.append("## 需要关注")
    lines.append("")

    # 检查胜率
    for h in horizons:
        summary = compute_summary(evaluations, horizon=h, n_recent=20)
        if summary.signal_count >= 5 and summary.outperform_rate < 40:
            lines.append(
                f"- ⚠️ T+{h} 最近胜率仅 {summary.outperform_rate:.0f}%，"
                "低于 40% 阈值，建议检查策略参数"
            )

    # 检查防守频率
    recent_signals = signals[:20]
    defensive_count = sum(1 for s in recent_signals if s.get("is_defensive", False))
    if len(recent_signals) >= 5 and defensive_count / len(recent_signals) > 0.3:
        lines.append(
            f"- ⚠️ 防守触发频率 {defensive_count}/{len(recent_signals)} "
            f"({defensive_count/len(recent_signals)*100:.0f}%)，"
            "MA60 可能过于敏感"
        )

    if not lines[-1].startswith("-"):
        lines.append("- 当前表现正常，无需特别关注")

    lines.append("")
    lines.append("---")
    lines.append(f"*生成时间: {datetime.now(HKT).strftime('%Y-%m-%d %H:%M')} HKT*")
    lines.append("")

    return "\n".join(lines)
