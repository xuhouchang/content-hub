"""
Phase 1 极简回测验证。

使用 vectorbt 进行向量化回测，验证策略历史表现。
与实盘策略解耦：通过 strategy/rotation 和 strategy/position 的接口运行。

不建完整 backtest 框架，只做一次运行验证。
"""

from __future__ import annotations

import logging
from datetime import timedelta

import pandas as pd
import numpy as np

from config.settings import get_settings, Settings

logger = logging.getLogger(__name__)


def run_quick_backtest(
    close_prices: dict[str, pd.Series],
    settings_override: Settings | None = None,
    verbose: bool = True,
) -> dict:
    """运行极简回测验证。

    流程：
    1. 从 strategy/rotation 获取信号
    2. 向量化遍历交易日，模拟每日调仓
    3. 生成净值曲线
    4. 计算收益/夏普/回撤/胜率

    Args:
        close_prices: dict[ticker -> Adj Close Series]
        settings_override: 配置覆盖
        verbose: 是否打印结果

    Returns:
        dict 包含 equity_curve 和 metrics
    """
    settings = settings_override or get_settings()

    from strategy.rotation import run_rotation
    from strategy.position import allocate
    from .metrics import summary_metrics

    # ── 1. 对齐日期索引 ─────────────────────────────────
    if not close_prices:
        logger.warning("无可用数据，跳过回测")
        return {}

    aligned = _align_close_prices(close_prices)
    if aligned.empty:
        return {}

    # ── 2. 按调仓频率生成信号 ───────────────────────────
    rebal_dates = _rebalance_dates(
        aligned.index,
        settings.backtest.rebalance_freq,
    )

    # ── 3. 回测循环 ─────────────────────────────────────
    positions: dict[str, float] = {t: 0.0 for t in aligned.columns}
    positions["CASH"] = 1.0  # 初始全现金

    equity = pd.Series(1.0, index=aligned.index)
    prev_date = None

    for date in equity.index:
        # 调仓日：重新计算信号
        if date in rebal_dates or prev_date is None:
            window = _lookback_window(aligned.loc[:date], min_days=60)

            if not window.empty:
                window_prices = {t: window[t] for t in window.columns}
                signal = run_rotation(window_prices, settings)
                alloc = allocate(
                    signal.selected_tickers,
                    signal.is_defensive,
                    max_weight=settings.position.max_single,
                )

                # 更新持仓权重
                for t in positions:
                    positions[t] = alloc.allocations.get(t, 0.0)
                positions["CASH"] = alloc.cash_weight

        # 计算当日收益
        if prev_date is not None:
            daily_ret = sum(
                positions.get(t, 0.0)
                * (aligned.loc[date, t] / aligned.loc[prev_date, t] - 1)
                for t in positions
                if t != "CASH"
                and aligned.loc[prev_date, t] > 0
            )
            if not pd.isna(daily_ret):
                equity[date] = equity[prev_date] * (1 + daily_ret)
            else:
                equity[date] = equity[prev_date]
            prev_date = date
        else:
            equity[date] = 1.0
            prev_date = date

    # ── 4. 计算指标 ─────────────────────────────────────
    benchmark_ticker = settings.universe.tickers[0]
    benchmark = aligned[benchmark_ticker] / aligned[benchmark_ticker].iloc[0]

    metrics = summary_metrics(equity, benchmark)

    result = {
        "equity_curve": equity,
        "benchmark_curve": benchmark,
        "metrics": metrics,
    }

    if verbose:
        _print_metrics(metrics)

    return result


def _align_close_prices(
    prices: dict[str, pd.Series],
) -> pd.DataFrame:
    """对齐所有标的为统一 DataFrame。"""
    df = pd.DataFrame(prices)
    df = df.dropna(how="all")
    df.columns = [str(c) for c in df.columns]
    return df


def _rebalance_dates(
    dates: pd.DatetimeIndex,
    freq: str,
) -> set:
    """生成调仓日期集合。"""
    rebal = dates.to_series().resample(freq).first().dropna()
    return set(rebal.index)


def _lookback_window(
    df: pd.DataFrame,
    min_days: int,
) -> pd.DataFrame:
    """获取最近 min_days 天的有效数据窗口。"""
    if len(df) < min_days:
        return df
    return df.iloc[-min_days:]


def _print_metrics(metrics: dict) -> None:
    """打印回测指标。"""
    print("=" * 50)
    print("回测结果")
    print("=" * 50)
    print(f"  总收益率:     {metrics.get('total_return_pct', 0):>8.2f}%")
    print(f"  年化收益率:   {metrics.get('annual_return_pct', 0):>8.2f}%")
    print(f"  年化波动率:   {metrics.get('annual_vol_pct', 0):>8.2f}%")
    print(f"  夏普比率:     {metrics.get('sharpe_ratio', 0):>8.4f}")
    print(f"  最大回撤:     {metrics.get('max_drawdown_pct', 0):>8.2f}%")
    print(f"  Calmar 比率:  {metrics.get('calmar_ratio', 0):>8.4f}")
    print(f"  胜率:         {metrics.get('win_rate_pct', 0):>8.2f}%")
    print(f"  基准收益率:   {metrics.get('benchmark_return_pct', 0):>8.2f}%")
    print(f"  超额收益:     {metrics.get('excess_return_pct', 0):>8.2f}%")
    print(f"  交易日数:     {metrics.get('trading_days', 0):>8d}")
    print("=" * 50)
