"""
趋势信号计算。

提供 MA 计算及价格-均线关系判断。
"""

from __future__ import annotations

import pandas as pd

from config.settings import get_settings


def compute_ma(
    prices: pd.Series,
    periods: list[int] | None = None,
) -> pd.DataFrame:
    """计算多周期移动平均线。

    Args:
        prices: 价格序列
        periods: MA 周期列表，默认 [20, 60]

    Returns:
        DataFrame，列如 ma_20, ma_60
    """
    if periods is None:
        settings = get_settings()
        periods = settings.trend.ma_periods

    result = pd.DataFrame(index=prices.index)
    for p in periods:
        result[f"ma_{p}"] = prices.rolling(window=p).mean()
    return result


def price_above_ma(
    prices: pd.Series,
    ma_period: int = 60,
) -> pd.Series:
    """判断价格是否高于指定 MA。

    返回布尔 Series，True = 价格在均线上方（上升趋势）。
    """
    ma = prices.rolling(window=ma_period).mean()
    return prices > ma


def price_vs_ma_pct(
    prices: pd.Series,
    ma_period: int = 60,
) -> pd.Series:
    """计算价格相对于 MA 的偏离百分比。

    正值 = 价格高于均线 (溢价)
    负值 = 价格低于均线 (折价)
    """
    ma = prices.rolling(window=ma_period).mean()
    return (prices - ma) / ma


def trend_state(
    prices: pd.Series,
    ma_periods: list[int] | None = None,
) -> str:
    """判断最新趋势状态标签。

    规则:
    - 上升: price > MA20 > MA60
    - 上升(弱势): price > MA20, 但 MA20 < MA60
    - 下跌: price < MA20 < MA60
    - 横盘: 其他

    Returns:
        "上升" | "上升(弱势)" | "下跌" | "横盘震荡"
    """
    if ma_periods is None:
        settings = get_settings()
        ma_periods = settings.trend.ma_periods

    last_price = prices.iloc[-1]

    mas = compute_ma(prices, ma_periods)
    last_mas = mas.iloc[-1]

    ma_short_col = f"ma_{ma_periods[0]}"
    ma_long_col = f"ma_{ma_periods[1]}"

    price_above_short = last_price > last_mas.get(ma_short_col, last_price * 0.99)
    price_above_long = last_price > last_mas.get(ma_long_col, last_price * 0.99)
    short_above_long = last_mas.get(ma_short_col, 0) > last_mas.get(ma_long_col, 1)

    if price_above_short and price_above_long and short_above_long:
        return "上升"
    elif price_above_short and not price_above_long:
        return "上升(弱势)"
    elif not price_above_short and not price_above_long and not short_above_long:
        return "下跌"
    else:
        return "横盘震荡"
