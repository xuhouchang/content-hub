"""
规则引擎。

趋势判断、防守触发等规则逻辑集中在此。
不直接决定仓位，仅输出规则信号供 position.py 使用。
"""

from __future__ import annotations

import pandas as pd

from config.settings import get_settings


def should_go_defensive(
    ticker: str,
    close: pd.Series,
    ma_period: int | None = None,
) -> bool:
    """判断是否应切换到防守资产。

    规则: 价格跌破指定 MA → 触发防守

    Args:
        ticker: ETF 代码（仅用于日志）
        close: 价格序列
        ma_period: MA 周期，默认从配置读取

    Returns:
        True = 触发防守（应切换至现金）
    """
    if ma_period is None:
        settings = get_settings()
        ma_period = settings.position.defensive_on_break_ma

    ma = close.rolling(window=ma_period).mean()

    if len(close) < ma_period or pd.isna(ma.iloc[-1]):
        return False  # 数据不足时不做防守

    return close.iloc[-1] <= ma.iloc[-1]


def is_defensive_ticker(ticker: str) -> bool:
    """判断 ticker 是否为防守资产。"""
    from config.universe import Universe
    return Universe.is_defensive(ticker)
