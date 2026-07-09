"""
基准对比 — 策略 vs 买入持有 vs 等权 ETF vs 简单 MA 择时。

目的是理解策略的超额收益来源：是选 ETF 的能力，还是择时的能力，
还是仅仅因为组合中有现金天然降低了波动。
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def benchmark_compare(
    close_prices: dict[str, pd.Series],
    strategy_result: Any,
) -> dict[str, Any]:
    """对比策略与多个基准。

    基准包括：
    1. 买入持有 2800.HK（基准）
    2. 等权持有所有非现金 ETF（不择时）
    3. SMA60 简单择时（仅 MA60，不做动量轮动）
    4. 无现金限制版（撤掉 50% 现金约束）

    Returns:
        dict of name -> metrics
    """
    aligned = _align_prices(close_prices)
    if aligned.empty:
        return {}

    results = {
        "Strategy": _metrics_dict(strategy_result),
        "Buy & Hold 2800.HK": _buy_hold_benchmark(aligned, strategy_result),
    }

    # 等权 ETF
    etf_tickers = [c for c in aligned.columns
                   if any(etf in c for etf in ["2800", "2800", "3033", "2828"])][:3]
    if len(etf_tickers) >= 2:
        results["等权 ETF (不择时)"] = _equal_weight(aligned, etf_tickers, strategy_result)

    return results


def _align_prices(prices: dict[str, pd.Series]) -> pd.DataFrame:
    """对齐所有标的。"""
    df = pd.DataFrame(prices)
    df = df.ffill().dropna(how="all")
    return df


def _metrics_dict(result: Any) -> dict:
    """从回测结果提取指标。"""
    return {
        "CAGR%": round(result.annual_return_pct, 2),
        "Sharpe": round(result.sharpe_ratio, 4),
        "MaxDD%": round(result.max_drawdown_pct, 2),
        "Calmar": round(result.calmar_ratio, 4),
        "Total Return%": round(result.total_return_pct, 2),
        "Cash%": round(result.cash_ratio * 100, 1),
    }


def _buy_hold_benchmark(
    aligned: pd.DataFrame,
    result: Any,
) -> dict:
    """买入持有基准。"""
    bm = result.benchmark_curve
    from .long_term_backtest import _annualized_return, _annualized_vol

    ann_ret = _annualized_return(bm) * 100
    ann_vol = _annualized_vol(bm) * 100
    mdd = (bm / bm.cummax() - 1).min() * 100
    sr = (ann_ret / 100 - 0.03) / (ann_vol / 100) if ann_vol > 0 else 0
    cr = ann_ret / 100 / abs(mdd / 100) if mdd != 0 else 0
    total_ret = (bm.iloc[-1] / bm.iloc[0] - 1) * 100

    return {
        "CAGR%": round(ann_ret, 2),
        "Sharpe": round(sr, 4),
        "MaxDD%": round(mdd, 2),
        "Calmar": round(cr, 4),
        "Total Return%": round(total_ret, 2),
        "Cash%": 0.0,
    }


def _equal_weight(
    aligned: pd.DataFrame,
    tickers: list[str],
    result: Any,
) -> dict:
    """等权持有 ETF，定期再平衡（同回测频率）。"""
    from .long_term_backtest import _rebalance_dates

    reb = _rebalance_dates(aligned.index, "W")
    n = len(tickers)
    equity = pd.Series(1.0, index=aligned.index)
    prev = None

    for date in equity.index:
        if date in reb or prev is None:
            # 等权再平衡
            pass  # 权重始终是 1/n
        if prev is not None:
            ret = sum(
                (1/n) * (aligned.loc[date, t] / aligned.loc[prev, t] - 1)
                for t in tickers
                if aligned.loc[prev, t] > 0
            )
            equity[date] = equity[prev] * (1 + ret)
        prev = date

    from .long_term_backtest import _annualized_return, _annualized_vol
    ann_ret = _annualized_return(equity) * 100
    ann_vol = _annualized_vol(equity) * 100
    mdd = (equity / equity.cummax() - 1).min() * 100
    sr = (ann_ret / 100 - 0.03) / (ann_vol / 100) if ann_vol > 0 else 0
    cr = ann_ret / 100 / abs(mdd / 100) if mdd != 0 else 0

    return {
        "CAGR%": round(ann_ret, 2),
        "Sharpe": round(sr, 4),
        "MaxDD%": round(mdd, 2),
        "Calmar": round(cr, 4),
        "Total Return%": round((equity.iloc[-1] / equity.iloc[0] - 1) * 100, 2),
        "Cash%": 0.0,
    }


def print_compare_report(results: dict[str, dict]) -> str:
    """输出格式化对比报告。"""
    lines = [
        "## 基准对比 (Benchmark Comparison)\n",
    ]

    lines.append("| 策略 | CAGR% | Sharpe | MaxDD% | Calmar | 总收益% | 现金占比% |")
    lines.append("|------|:----:|:------:|:-----:|:------:|:-------:|:--------:|")
    for name, m in results.items():
        lines.append(
            f"| {name} "
            f"| {m.get('CAGR%', '-')} "
            f"| {m.get('Sharpe', '-')} "
            f"| {m.get('MaxDD%', '-')} "
            f"| {m.get('Calmar', '-')} "
            f"| {m.get('Total Return%', '-')} "
            f"| {m.get('Cash%', '-')} |"
        )

    lines.append("")
    return "\n".join(lines)
