"""
历史建议评估。

对每条历史 signal log 计算 T+1, T+3, T+5 的表现。
仅依赖已发生的数据——如果某 horizon 未来交易日不够，跳过。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import pandas as pd

from config.settings import get_settings

logger = logging.getLogger(__name__)


@dataclass
class Evaluation:
    """单条信号在某 horizon 上的评估结果。"""
    signal_date: str
    horizon: int                 # 1, 3, 5
    horizon_trading_days: int    # 实际使用的交易日数（非自然日）
    portfolio_return: float
    benchmark_return: float
    excess_return: float
    is_outperform: bool          # excess_return > 0
    is_absolute_positive: bool   # portfolio_return > 0
    hit: bool                    # both conditions met


@dataclass
class SummaryStats:
    """近 N 条信号的综合统计。"""
    signal_count: int
    outperform_rate: float       # 胜率 (%)
    avg_excess_return: float     # 平均超额收益
    absolute_positive_rate: float
    defensive_ratio: float       # 防守触发占比


def evaluate_all(
    signals: list[dict],
    close_prices: dict[str, pd.Series],
    horizons: list[int] | None = None,
) -> list[Evaluation]:
    """对所有历史信号，计算各 horizon 的表现。

    Args:
        signals: signal_logger.load_signals() 的输出
        close_prices: dict[ticker -> Adj Close Series]（包含全量历史数据）
        horizons: 评估 horizon 列表，默认 [1, 3, 5]

    Returns:
        所有能够计算的 Evaluation 列表（跳过未来数据不足的记录）
    """
    if horizons is None:
        settings = get_settings()
        horizons = [1, 3, 5]

    results: list[Evaluation] = []
    settings = get_settings()
    benchmark = settings.universe.tickers[0]

    for sig in signals:
        signal_date_str = sig["signal_date"]
        weights = sig.get("recommended_weights", {})
        is_defensive = sig.get("is_defensive", False)

        # 找到 signal_date 在 aligned 数据中的位置
        signal_dt = pd.Timestamp(signal_date_str)

        for horizon in horizons:
            eval_result = _evaluate_single(
                signal_date=signal_dt,
                horizon=horizon,
                weights=weights,
                is_defensive=is_defensive,
                benchmark_ticker=benchmark,
                close_prices=close_prices,
            )
            if eval_result is not None:
                results.append(eval_result)

    return results


HORIZON_LABELS: dict[int, str] = {
    1: "T+1（次日）",
    3: "T+3（3日）",
    5: "T+5（周内）",
    7: "T+7（一周）",
}


def _evaluate_single(
    signal_date: pd.Timestamp,
    horizon: int,
    weights: dict[str, float],
    is_defensive: bool,
    benchmark_ticker: str,
    close_prices: dict[str, pd.Series],
) -> Evaluation | None:
    """计算单条信号在单个 horizon 上的表现。

    使用规则：
    - 找到 signal_date 之后的第 horizon 个交易日
    - 如果 horizon 个交易日不够 → 跳过
    - CASH 收益按 0 计算
    """
    benchmark_series = close_prices.get(benchmark_ticker)
    if benchmark_series is None:
        return None

    # 找到 signal_date 在 benchmark 序列中的位置
    filtered = benchmark_series.index[benchmark_series.index >= signal_date]
    if len(filtered) < 2:
        return None

    start_idx = benchmark_series.index.get_indexer([filtered[0]], method="nearest")[0]
    end_idx = start_idx + horizon

    if end_idx >= len(benchmark_series):
        return None  # 未来数据不足

    # 实际使用的交易日数量
    trading_days_used = len(benchmark_series.iloc[start_idx:end_idx + 1]) - 1

    # 信号发出日的价格（用当天收盘作为买入基准）
    entry_price_date = filtered[0]
    exit_price_date = benchmark_series.index[end_idx]

    # 组合收益
    portfolio_ret = _portfolio_return(
        weights=weights,
        entry_date=entry_price_date,
        exit_date=exit_price_date,
        close_prices=close_prices,
    )

    # 基准收益
    bp_entry = benchmark_series.loc[entry_price_date]
    bp_exit = benchmark_series.loc[exit_price_date]
    benchmark_ret = bp_exit / bp_entry - 1

    excess = portfolio_ret - benchmark_ret
    is_outperform = excess > 0
    is_abs_pos = portfolio_ret > 0
    hit = is_outperform and is_abs_pos

    signal_date_str = signal_date.strftime("%Y-%m-%d")

    return Evaluation(
        signal_date=signal_date_str,
        horizon=horizon,
        horizon_trading_days=trading_days_used,
        portfolio_return=portfolio_ret,
        benchmark_return=benchmark_ret,
        excess_return=excess,
        is_outperform=is_outperform,
        is_absolute_positive=is_abs_pos,
        hit=hit,
    )


def _portfolio_return(
    weights: dict[str, float],
    entry_date: pd.Timestamp,
    exit_date: pd.Timestamp,
    close_prices: dict[str, pd.Series],
) -> float:
    """计算组合收益。CASH 收益=0。"""
    total_ret = 0.0
    for ticker, weight in weights.items():
        if ticker == "CASH" or weight == 0:
            continue

        series = close_prices.get(ticker)
        if series is None or entry_date not in series.index or exit_date not in series.index:
            continue

        entry_price = series.loc[entry_date]
        exit_price = series.loc[exit_date]

        if entry_price > 0:
            ticker_ret = exit_price / entry_price - 1
            total_ret += weight * ticker_ret

    return total_ret


def compute_summary(
    evaluations: list[Evaluation],
    horizon: int = 1,
    n_recent: int = 20,
) -> SummaryStats:
    """计算近 N 条信号在指定 horizon 上的汇总统计。"""
    filtered = [e for e in evaluations if e.horizon == horizon]
    filtered = filtered[:n_recent]  # 取最近的 N 条

    if not filtered:
        return SummaryStats(
            signal_count=0,
            outperform_rate=0.0,
            avg_excess_return=0.0,
            absolute_positive_rate=0.0,
            defensive_ratio=0.0,
        )

    n = len(filtered)
    n_outperform = sum(1 for e in filtered if e.is_outperform)
    n_abs_pos = sum(1 for e in filtered if e.is_absolute_positive)

    avg_excess = sum(e.excess_return for e in filtered) / n

    return SummaryStats(
        signal_count=n,
        outperform_rate=(n_outperform / n) * 100,
        avg_excess_return=avg_excess,
        absolute_positive_rate=(n_abs_pos / n) * 100,
        defensive_ratio=0.0,  # 由调用方单独计算
    )
