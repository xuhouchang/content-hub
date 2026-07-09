"""
错误建议归因。

对 evaluator 判断为未跑赢（!is_outperform）的信号，用规则做归因。
不使用 LLM，仅依赖可量化的信号和价格关系。
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from config.settings import get_settings
from config.universe import Universe

logger = logging.getLogger(__name__)


def attribute(
    signal: dict,
    evaluation: Any,
    close_prices: dict[str, pd.Series],
) -> list[str]:
    """对一条失效信号进行归因分析。

    Args:
        signal: signal_logger 中的一条原始记录
        evaluation: evaluator 中的对应 Evaluation 对象
        close_prices: 全量价格数据

    Returns:
        归因原因列表
    """
    reasons: list[str] = []

    signal_date = signal["signal_date"]
    selected = signal.get("selected_etf", [])
    signal_dt = pd.Timestamp(signal_date)

    if not selected:
        reasons.append("防守触发：全仓现金")
        return reasons

    benchmark_ticker = signal.get("benchmark", "")
    settings = get_settings()

    for ticker in selected:
        series = close_prices.get(ticker)
        if series is None:
            continue

        # 找到 signal_date 之后的数据
        post_signal = series[series.index >= signal_dt]
        if len(post_signal) < 2:
            continue

        entry_price = post_signal.iloc[0]
        ma_short = series.rolling(window=20).mean()
        ma_long = series.rolling(window=60).mean()

        # 归因 1: 跌破 MA20
        if entry_price > 0 and not pd.isna(ma_short.loc[signal_dt]):
            if entry_price <= ma_short.loc[signal_dt]:
                reasons.append(f"{ticker} 信号发出日已跌破 MA20")

        # 归因 2: 跌破 MA60（防守线）
        if entry_price > 0 and not pd.isna(ma_long.loc[signal_dt]):
            if entry_price <= ma_long.loc[signal_dt]:
                reasons.append(f"{ticker} 信号发出日已跌破 MA60（防守线）")

        # 归因 3: 波动率过高
        score_snapshot = signal.get("score_snapshot", {})
        ticker_scores = score_snapshot.get(ticker, {})
        hv = ticker_scores.get("hv_20d", 0)
        if hv > 0.30:
            reasons.append(f"{ticker} 波动率偏高 ({hv*100:.1f}%)，信号可能不稳定")

    # 归因 4: 市场整体下跌
    benchmark_series = close_prices.get(benchmark_ticker)
    if benchmark_series is not None:
        post_benchmark = benchmark_series[benchmark_series.index >= signal_dt]
        if len(post_benchmark) >= 2:
            benchmark_ret = post_benchmark.iloc[-1] / post_benchmark.iloc[0] - 1
            if benchmark_ret < -0.02:
                reasons.append(f"市场整体下跌（{benchmark_ticker} T+期间 {benchmark_ret*100:.1f}%）")

    # 归因 5: 动量信号失效
    if signal.get("is_defensive", False):
        reasons.append("防守触发但市场反弹，错过机会")
    else:
        score = signal.get("score_snapshot", {})
        top_ticker = ""
        for t, s in score.items():
            if s.get("rank", 999) == 1:
                top_ticker = t
                break
        if top_ticker and top_ticker in selected:
            reasons.append(
                f"动量信号失效：{top_ticker} 发出的信号未能在后续交易日产生正超额"
            )

    if not reasons:
        reasons.append("归因失败：无法确定具体原因")

    return reasons
