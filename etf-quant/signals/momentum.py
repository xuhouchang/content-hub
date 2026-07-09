"""
动量信号计算。

提供多周期收益率计算，作为轮动策略的输入因子。
"""

from __future__ import annotations

import pandas as pd

from config.settings import get_settings, Settings


def compute_returns(
    prices: pd.Series,
    periods: list[int] | None = None,
) -> pd.DataFrame:
    """计算多周期收益率。

    Args:
        prices: 价格序列 (Adj Close)
        periods: 计算周期列表，默认从配置读取 [20, 60]

    Returns:
        DataFrame，列如 ret_20, ret_60
    """
    if periods is None:
        settings = get_settings()
        periods = settings.momentum.periods

    result = pd.DataFrame(index=prices.index)
    for p in periods:
        result[f"ret_{p}"] = prices.pct_change(periods=p)
    return result


def latest_momentum_scores(
    prices: dict[str, pd.Series],
    periods: list[int] | None = None,
    weights: list[float] | None = None,
    settings_override: Settings | None = None,
) -> pd.DataFrame:
    """计算所有 ETF 最新交易日的动量得分（用于日报/建议）。

    Args:
        prices: dict[ticker -> Adj Close Series]
        periods: 周期列表
        weights: 各周期权重，需与 periods 长度一致
        settings_override: 配置覆盖

    Returns:
        DataFrame, index=ticker, columns=ret_20, ret_60, score
    """
    settings = settings_override or get_settings()
    if periods is None:
        periods = settings.momentum.periods
    if weights is None:
        weights = settings.momentum.weights

    records: list[dict] = []
    for ticker, series in prices.items():
        if series.empty:
            continue
        rets = compute_returns(series, periods)
        latest = rets.iloc[-1]

        record = {"ticker": ticker}
        score = 0.0
        for p, w in zip(periods, weights):
            col = f"ret_{p}"
            record[col] = latest.get(col, 0.0)
            score += w * record[col]

        record["momentum_score"] = score
        records.append(record)

    result = pd.DataFrame(records)
    if not result.empty:
        result = result.set_index("ticker")
        result = result.sort_values("momentum_score", ascending=False)
    return result
