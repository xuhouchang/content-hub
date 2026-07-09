"""
表现指标计算。

纯函数，不依赖回测引擎。
输入 equity curve 或 return series，输出指标 dict。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def annualized_return(
    equity: pd.Series,
    trading_days: int = 252,
) -> float:
    """年化收益率。"""
    if len(equity) < 2:
        return 0.0
    total_return = equity.iloc[-1] / equity.iloc[0]
    years = len(equity) / trading_days
    if years <= 0:
        return 0.0
    return float(total_return ** (1.0 / years) - 1.0)


def annualized_volatility(
    equity: pd.Series,
    trading_days: int = 252,
) -> float:
    """年化波动率。"""
    returns = equity.pct_change().dropna()
    if len(returns) < 2:
        return 0.0
    return float(returns.std() * np.sqrt(trading_days))


def sharpe_ratio(
    equity: pd.Series,
    risk_free_rate: float = 0.03,
    trading_days: int = 252,
) -> float:
    """夏普比率。"""
    ret = annualized_return(equity, trading_days)
    vol = annualized_volatility(equity, trading_days)
    if vol == 0:
        return 0.0
    return (ret - risk_free_rate) / vol


def max_drawdown(equity: pd.Series) -> float:
    """最大回撤。"""
    if len(equity) < 2:
        return 0.0
    cumulative_max = equity.cummax()
    drawdown = (equity - cumulative_max) / cumulative_max
    return float(drawdown.min())


def calmar_ratio(
    equity: pd.Series,
    trading_days: int = 252,
) -> float:
    """Calmar 比率 = 年化收益 / 最大回撤绝对值。"""
    ret = annualized_return(equity, trading_days)
    mdd = max_drawdown(equity)
    if mdd == 0:
        return 0.0
    return ret / abs(mdd)


def win_rate(returns: pd.Series) -> float:
    """胜率（正收益交易日占比）。"""
    if len(returns) < 2:
        return 0.0
    return float((returns > 0).sum() / len(returns))


def summary_metrics(
    equity: pd.Series,
    benchmark_equity: pd.Series | None = None,
    trading_days: int = 252,
) -> dict[str, float]:
    """全套指标汇总。

    Args:
        equity: 策略净值曲线
        benchmark_equity: 基准净值（可选），用于对比
        trading_days: 年化交易日数

    Returns:
        dict of metric_name -> value
    """
    returns = equity.pct_change().dropna()

    metrics = {
        "total_return_pct": float((equity.iloc[-1] / equity.iloc[0] - 1) * 100),
        "annual_return_pct": annualized_return(equity, trading_days) * 100,
        "annual_vol_pct": annualized_volatility(equity, trading_days) * 100,
        "sharpe_ratio": sharpe_ratio(equity, risk_free_rate=0.03, trading_days=trading_days),
        "max_drawdown_pct": max_drawdown(equity) * 100,
        "calmar_ratio": calmar_ratio(equity, trading_days),
        "win_rate_pct": win_rate(returns) * 100,
        "trading_days": len(equity),
    }

    if benchmark_equity is not None:
        metrics["benchmark_return_pct"] = float(
            (benchmark_equity.iloc[-1] / benchmark_equity.iloc[0] - 1) * 100
        )
        metrics["excess_return_pct"] = (
            metrics["total_return_pct"] - metrics["benchmark_return_pct"]
        )

    return metrics
