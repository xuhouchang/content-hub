"""
市场环境分析 — Regime Detection。

自动识别不同市场状态，评估策略在各 regime 下的表现。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class RegimeStats:
    """单 regime 的表现统计。"""
    label: str
    start: str
    end: str
    trading_days: int
    strategy_return_pct: float
    benchmark_return_pct: float
    excess_return_pct: float
    annual_return_pct: float
    annual_vol_pct: float
    sharpe_ratio: float
    max_drawdown_pct: float
    calmar_ratio: float
    defensive_trigger_rate: float    # 该区间内防守触发比例
    win_rate_pct: float
    avg_position_pct: float          # 平均仓位（1 - cash_ratio）


@dataclass
class RegimeAnalysis:
    """完整市场环境分析结果。"""
    regimes: list[RegimeStats]
    full_summary: dict
    regime_labels: pd.Series                     # date -> label
    regime_boundaries: list[tuple[str, str, str]]  # (label, start, end)


def analyze_market_regimes(
    result: Any,
    close_prices: dict[str, pd.Series],
) -> RegimeAnalysis:
    """分析策略在不同市场环境下的表现。

    识别的 Regime:
    1. 单边上涨 (Bull)
    2. 单边下跌 (Bear)
    3. 高频震荡 (Sideways / High Vol)
    4. AI 科技主线行情 (Tech Rally)
    5. 美债快速上行阶段 (Rising Yield)
    6. 港股流动性低迷阶段 (Low Liquidity)

    Args:
        result: LongTermResult
        close_prices: 价格数据

    Returns:
        RegimeAnalysis
    """
    aligned = result.price_data

    # ── Regime 1-3: 通过基准市场状态划分 ──
    benchmark = aligned.iloc[:, 0]

    # 滑动窗口：60 天基准收益 + 滚动波动率
    bench_ret_60d = benchmark.pct_change(60)
    bench_vol_60d = benchmark.pct_change().rolling(60).std() * np.sqrt(252)

    # 定义 regime
    regimes_raw: list[tuple[str, pd.Series]] = []

    # Regime 1: 单边上涨
    is_bull = (bench_ret_60d > 0.08) & (bench_vol_60d < 0.25)
    regimes_raw.append(("单边上涨 (Bull)", is_bull))

    # Regime 2: 单边下跌
    is_bear = (bench_ret_60d < -0.08) & (bench_vol_60d < 0.30)
    regimes_raw.append(("单边下跌 (Bear)", is_bear))

    # Regime 3: 高频震荡
    is_sideways = (bench_ret_60d.abs() < 0.05) & (bench_vol_60d > 0.20)
    regimes_raw.append(("高频震荡 (Sideways/High Vol)", is_sideways))

    # Regime 4: AI 科技主线行情 → 3033.HK 相对于大盘的超额表现
    if "03033.HK" in aligned.columns or "3033.HK" in aligned.columns:
        tech_ticker = "3033.HK" if "3033.HK" in aligned.columns else "3033.HK"
        # 先找 ticker 的实际列名
    else:
        tech_ticker = None

    # 查找 tech ticker 的映射
    tech_col = None
    for col in aligned.columns:
        if col.replace(".HK", "").replace("0", "") in ("3033", "3033", "3033"):
            tech_col = col
            break
    if tech_col is None:
        # 尝试 ex-ante 转换
        ticker_map = {t: str(t) for t in close_prices.keys()}
        for t, c in ticker_map.items():
            if "3033" in t or "3033" in c:
                tech_col = c if c in aligned.columns else t
                break

    if not tech_col or tech_col not in aligned.columns:
        logger.warning("3033.HK 未在数据中，跳过 AI 科技主线分析")
    else:
        tech_excess = aligned[tech_col].pct_change(60) - bench_ret_60d
        is_tech = (tech_excess > 0.10)
        regimes_raw.append(("AI/科技主线 (Tech Rally)", is_tech))

    # Regime 5 & 6 需要外部数据，标记为"需要外部数据"
    # 用代理：港股下跌+高波动 = 流动性低迷的近似
    proxy_low_liq = is_bear & (bench_vol_60d > bench_vol_60d.median())
    regimes_raw.append(("港股流动性低迷≈", proxy_low_liq))

    # 构建 composite regime label（每段取占比最高的 regime）
    # 按月分段
    regime_labels = _composite_regime_labels(aligned.index, regimes_raw)

    # ── 按连续 regime 区间计算统计 ──
    regimes_out = _compute_regime_stats(
        result, aligned, regime_labels
    )

    return RegimeAnalysis(
        regimes=regimes_out,
        full_summary={},
        regime_labels=regime_labels,
        regime_boundaries=_find_boundaries(regime_labels),
    )


def _composite_regime_labels(
    dates: pd.DatetimeIndex,
    regimes_raw: list[tuple[str, pd.Series]],
) -> pd.Series:
    """按月分段，取每段中占比最高的 regime。"""
    s = pd.Series("其他", index=dates)
    monthly_groups = s.groupby(dates.to_period("M"))

    for period, group in monthly_groups:
        idx = group.index
        scores = {}
        for label, mask in regimes_raw:
            if mask.empty:
                continue
            common = mask.reindex(idx, method=None)
            if common.isna().all():
                continue
            scores[label] = common.sum() / len(idx)

        if scores:
            best = max(scores, key=scores.get)
            if scores[best] > 0.25:  # 至少覆盖该月 25% 交易日
                s.loc[idx] = best

    return s


def _compute_regime_stats(
    result: Any,
    aligned: pd.DataFrame,
    regime_labels: pd.Series,
) -> list[RegimeStats]:
    """对每个连续 regime 区间计算统计。"""
    stats_list: list[RegimeStats] = []

    # 找到前后一致的 regime block
    unique_labels = regime_labels.unique()
    for label in unique_labels:
        if label == "其他":
            continue

        mask = regime_labels == label
        regime_dates = mask[mask].index

        if len(regime_dates) < 10:  # 不足 2 周跳过
            continue

        # 此区间内的策略权益
        eq = result.equity_curve.reindex(regime_dates)
        bm = result.benchmark_curve.reindex(regime_dates)

        eq = eq.dropna()
        bm = bm.dropna()
        if len(eq) < 5:
            continue

        start_dt = eq.index[0].strftime("%Y-%m-%d")
        end_dt = eq.index[-1].strftime("%Y-%m-%d")

        from .long_term_backtest import _annualized_return, _annualized_vol
        from .metrics import max_drawdown, sharpe_ratio

        ann_ret = _annualized_return(eq) * 100
        ann_vol = _annualized_vol(eq) * 100
        mdd = max_drawdown(eq) * 100
        sr = (ann_ret / 100 - 0.03) / (ann_vol / 100) if ann_vol > 0 else 0
        cr = ann_ret / 100 / abs(mdd / 100) if mdd != 0 else 0

        strat_ret = (eq.iloc[-1] / eq.iloc[0] - 1) * 100
        bench_ret = (bm.iloc[-1] / bm.iloc[0] - 1) * 100
        excess = strat_ret - bench_ret

        # 防守触发率
        def_dates = set(d.strftime("%Y-%m-%d") for d in result.defensive_calls)
        regime_str_dates = set(d.strftime("%Y-%m-%d") for d in regime_dates)
        def_in_regime = len(def_dates & regime_str_dates)
        def_rate = def_in_regime / max(len(regime_dates), 1)

        # 平均仓位
        cash_dates_set = set(d.strftime("%Y-%m-%d") for d in result.cash_days)
        cash_in_regime = len(cash_dates_set & regime_str_dates)
        avg_position = (1 - cash_in_regime / max(len(regime_dates), 1)) * 100

        # 月度胜率
        rets = eq.pct_change().dropna()
        monthly = rets.groupby(rets.index.to_period("M")).apply(lambda x: (1 + x).prod() - 1)
        win_rate = (monthly > 0).sum() / max(len(monthly), 1) * 100

        stats_list.append(RegimeStats(
            label=label,
            start=start_dt,
            end=end_dt,
            trading_days=len(eq),
            strategy_return_pct=strat_ret,
            benchmark_return_pct=bench_ret,
            excess_return_pct=excess,
            annual_return_pct=ann_ret,
            annual_vol_pct=ann_vol,
            sharpe_ratio=sr,
            max_drawdown_pct=mdd,
            calmar_ratio=cr,
            defensive_trigger_rate=def_rate * 100,
            win_rate_pct=win_rate,
            avg_position_pct=avg_position,
        ))

    return stats_list


def _find_boundaries(labels: pd.Series) -> list[tuple[str, str, str]]:
    """找连续区块边界 (label, start, end)。"""
    boundaries: list[tuple[str, str, str]] = []
    if labels.empty:
        return boundaries

    prev_label = labels.iloc[0]
    start = labels.index[0]

    for date, label in labels.items():
        if label != prev_label:
            boundaries.append((prev_label, start.strftime("%Y-%m-%d"), date.strftime("%Y-%m-%d")))
            prev_label = label
            start = date
    boundaries.append((prev_label, start.strftime("%Y-%m-%d"), labels.index[-1].strftime("%Y-%m-%d")))

    return boundaries


def print_regime_report(analysis: RegimeAnalysis) -> str:
    """输出格式化 Markdown regime 报告。"""
    lines = [
        "## 市场环境分析 (Market Regime Analysis)\n",
    ]

    # 按返回排序
    sorted_regimes = sorted(analysis.regimes, key=lambda r: r.strategy_return_pct, reverse=True)

    lines.append("| Regime | 区间 | 天数 | 策略收益 | 基准收益 | 超额 | CAGR | Sharpe | 最大回撤 | 防守率 | 平均仓位 |")
    lines.append("|--------|------|:---:|:--------:|:--------:|:----:|:----:|:------:|:-------:|:-----:|:-------:|")
    for r in sorted_regimes:
        lines.append(
            f"| {r.label} | {r.start[:7]}~{r.end[:7]} "
            f"| {r.trading_days} "
            f"| {r.strategy_return_pct:+.1f}% "
            f"| {r.benchmark_return_pct:+.1f}% "
            f"| {r.excess_return_pct:+.1f}% "
            f"| {r.annual_return_pct:.1f}% "
            f"| {r.sharpe_ratio:.2f} "
            f"| {r.max_drawdown_pct:.1f}% "
            f"| {r.defensive_trigger_rate:.0f}% "
            f"| {r.avg_position_pct:.0f}% |"
        )

    lines.append("")
    return "\n".join(lines)
