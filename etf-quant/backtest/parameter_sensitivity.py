"""
参数敏感性分析 — 哪些参数窗口稳定，哪些过拟合。

测试维度：
- MA 防守周期: [40, 50, 60, 80, 120]
- 动量窗口: [10d/50d, 20d/60d, 40d/80d, 60d/120d]

输出：
- 参数热力图 (CAGR, Sharpe, MaxDD, Calmar)
- 稳定参数区间识别
- 过拟合风险评估
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class SensitivityResult:
    """参数敏感性分析结果。"""
    grid: pd.DataFrame                      # index: param combo, columns: metrics
    param_names: list[str]                  # 参数名称
    stable_region: dict                     # 推荐的稳定参数区间
    overfit_warning: str | None             # 过拟合警示


SENSITIVITY_METRICS = [
    "annual_return_pct", "sharpe_ratio",
    "max_drawdown_pct", "calmar_ratio",
    "excess_return_pct", "cash_ratio",
    "win_rate_pct", "defensive_count",
]


def run_sensitivity(
    close_prices: dict[str, pd.Series],
    ma_range: list[int] | None = None,
    mom_window_pairs: list[tuple[int, int]] | None = None,
) -> SensitivityResult:
    """运行参数网格扫描。

    Args:
        close_prices: 价格数据
        ma_range: MA 防守周期列表，默认 [40, 50, 60, 80, 120]
        mom_window_pairs: 动量窗口对，默认对应权重 [0.4, 0.6]

    Returns:
        SensitivityResult
    """
    ma_list = ma_range or [40, 50, 60, 80, 120]

    # 动量窗口对： (短周期, 长周期) 与对应权重对 [0.4, 0.6]
    mom_configs = mom_window_pairs or [
        ([10, 50], [0.4, 0.6]),
        ([20, 60], [0.4, 0.6]),
        ([40, 80], [0.4, 0.6]),
        ([60, 120], [0.4, 0.6]),
    ]

    from .long_term_backtest import run_long_backtest

    rows: list[dict] = []
    for ma in ma_list:
        for m_periods, m_weights in mom_configs:
            label = f"MA{ma}_mom{m_periods[0]}-{m_periods[1]}"
            try:
                result = run_long_backtest(
                    close_prices,
                    ma_period=ma,
                    momentum_periods=m_periods,
                    momentum_weights=m_weights,
                )
                rows.append({
                    "ma": ma,
                    "mom_short": m_periods[0],
                    "mom_long": m_periods[1],
                    "label": label,
                    "annual_return_pct": round(result.annual_return_pct, 2),
                    "sharpe_ratio": round(result.sharpe_ratio, 4),
                    "max_drawdown_pct": round(result.max_drawdown_pct, 2),
                    "calmar_ratio": round(result.calmar_ratio, 4),
                    "excess_return_pct": round(result.excess_return_pct, 2),
                    "cash_ratio": round(result.cash_ratio * 100, 1),
                    "win_rate_pct": round(result.win_rate_pct, 1),
                    "defensive_count": result.defensive_count,
                    "total_return_pct": round(result.total_return_pct, 2),
                })
                logger.info("  %-25s CAGR=%.2f%% Sharpe=%.4f MaxDD=%.1f%% Calmar=%.4f",
                            label, rows[-1]["annual_return_pct"], rows[-1]["sharpe_ratio"],
                            rows[-1]["max_drawdown_pct"], rows[-1]["calmar_ratio"])
            except Exception as e:
                logger.warning("  %s failed: %s", label, e)
                rows.append({
                    "ma": ma, "mom_short": m_periods[0], "mom_long": m_periods[1],
                    "label": label, **{m: None for m in SENSITIVITY_METRICS},
                    "total_return_pct": None,
                })

    grid = pd.DataFrame(rows)

    # ── 稳定区间识别 ──
    stable = _identify_stable_region(grid)

    # ── 过拟合风险 ──
    overfit_warning = _detect_overfit(grid)

    return SensitivityResult(
        grid=grid,
        param_names=["ma", "mom_short", "mom_long"],
        stable_region=stable,
        overfit_warning=overfit_warning,
    )


def _identify_stable_region(grid: pd.DataFrame) -> dict:
    """识别参数区间中表现稳定的区域。

    稳定 = 相邻参数组合的指标变化不大 + 回撤可控。
    """
    if grid.empty or grid["sharpe_ratio"].isna().all():
        return {"stable_ma_range": [], "stable_mom_range": [], "notes": "数据不足"}

    # 按 MA 分组统计
    ma_groups = grid.groupby("ma").agg({
        "annual_return_pct": ["mean", "std"],
        "sharpe_ratio": ["mean", "std"],
        "max_drawdown_pct": ["mean", "std"],
        "calmar_ratio": ["mean", "std"],
    }).round(2)

    # 找 sharpe > median 且 calmar > median 的 MA
    median_sr = grid["sharpe_ratio"].median()
    median_cr = grid["calmar_ratio"].median()
    median_dd = grid["max_drawdown_pct"].median()

    # 按 MA 维度看哪些 MA 表现稳定
    ma_summary = grid.groupby("ma").agg(
        sharpe_mean=("sharpe_ratio", "mean"),
        sharpe_std=("sharpe_ratio", "std"),
        calmar_mean=("calmar_ratio", "mean"),
        calmar_std=("calmar_ratio", "std"),
        maxdd_mean=("max_drawdown_pct", "mean"),
        excess_mean=("excess_return_pct", "mean"),
        cash_ratio_mean=("cash_ratio", "mean"),
    ).round(2)

    # 推荐的 MA 区间：Sharpe 稳定且回撤可控
    good = ma_summary[
        (ma_summary["sharpe_mean"] >= median_sr)
        & (ma_summary["maxdd_mean"] <= median_dd * 1.1)
    ]
    good_ma = sorted(good.index.tolist())

    # 按 momentum 分组
    mom_groups = grid.groupby(["mom_short", "mom_long"]).agg(
        sharpe_mean=("sharpe_ratio", "mean"),
        sharpe_std=("sharpe_ratio", "std"),
        calmar_mean=("calmar_ratio", "mean"),
        maxdd_mean=("max_drawdown_pct", "mean"),
    ).round(2)

    return {
        "stable_ma_range": good_ma,
        "ma_summary": ma_summary.to_dict("index"),
        "stable_ma_condition": f"Sharpe >= {median_sr:.4f} & MaxDD <= {median_dd*1.1:.1f}%",
        "notes": "推荐选择跨 MA 值表现稳定的参数，而不是单点最优。",
    }


def _detect_overfit(grid: pd.DataFrame) -> str | None:
    """检测过拟合风险。

    规则：
    - 最佳参数组合与次佳差距 > 0.3 Sharpe → 潜在过拟合
    - 各 MA 之间 Sharpe 标准偏差 > 0.3 → 对参数敏感
    """
    if grid.empty or grid["sharpe_ratio"].isna().all():
        return "数据不足，无法评估"

    warnings: list[str] = []

    sorted_grid = grid.sort_values("sharpe_ratio", ascending=False)
    top_sr = sorted_grid.iloc[0]["sharpe_ratio"]
    second_sr = sorted_grid.iloc[1]["sharpe_ratio"] if len(sorted_grid) > 1 else top_sr

    if top_sr - second_sr > 0.3:
        warnings.append(
            f"⚠️ 最佳参数 (Sharpe={top_sr:.4f}) 与次佳 (Sharpe={second_sr:.4f}) "
            f"差距 {top_sr-second_sr:.4f} > 0.3，潜在过拟合信号"
        )

    # 各 MA 的 Sharpe 稳定性
    ma_sr_std = grid.groupby("ma")["sharpe_ratio"].std()
    high_var_ma = ma_sr_std[ma_sr_std > 0.3]
    for ma, std in high_var_ma.items():
        warnings.append(
            f"⚠️ MA{ma} 在不同动量窗口下的 Sharpe 标准差 {std:.4f} > 0.3，对该参数敏感"
        )

    if not warnings:
        return "✅ 参数敏感性较低，策略跨参数区间稳定。"

    return "\n".join(warnings)


def print_sensitivity_report(result: SensitivityResult) -> str:
    """输出格式化参数敏感性报告。"""
    lines = [
        "## 参数敏感性分析\n",
        "### 网格扫描结果\n",
    ]

    df = result.grid.copy()
    # 保留核心指标
    display_cols = ["label", "annual_return_pct", "sharpe_ratio",
                    "max_drawdown_pct", "calmar_ratio", "excess_return_pct",
                    "cash_ratio", "win_rate_pct", "defensive_count"]
    display = df[display_cols].round(2).copy()
    display.columns = ["参数", "CAGR%", "Sharpe", "MaxDD%", "Calmar",
                       "超额%", "现金占比%", "胜率%", "防守次数"]

    lines.append(f"```\n{display.to_string(index=False)}\n```\n")
    lines.append("")

    # 稳定区间
    stable = result.stable_region
    if stable.get("stable_ma_range"):
        lines.append(f"**稳定 MA 区间**: {stable['stable_ma_range']}")
        lines.append(f"**筛选条件**: {stable.get('stable_ma_condition', '')}")
        lines.append("")
    lines.append(f"{stable.get('notes', '')}\n")

    # 过拟合警告
    if result.overfit_warning:
        lines.append(f"**过拟合评估**:\n\n{result.overfit_warning}\n")

    return "\n".join(lines)
