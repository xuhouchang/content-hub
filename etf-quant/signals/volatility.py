"""
波动率信号计算。

提供历史波动率（HV）和 ATR 计算。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from config.settings import get_settings, Settings


def historical_volatility(
    prices: pd.Series,
    window: int | None = None,
    annualize: bool = True,
) -> pd.Series:
    """计算滚动历史波动率（年化）。

    Args:
        prices: 价格序列（Adj Close）
        window: 滚动窗口，默认从配置读取
        annualize: 是否年化

    Returns:
        Series, 日波动率（0.01 = 1%）或年化波动率
    """
    if window is None:
        settings = get_settings()
        window = settings.volatility.window

    log_returns = np.log(prices / prices.shift(1))
    rolling_std = log_returns.rolling(window=window).std()

    if annualize:
        settings = get_settings()
        factor = np.sqrt(settings.volatility.annualization_factor)
        return rolling_std * factor

    return rolling_std


def atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.Series:
    """计算平均真实波幅 (Average True Range)。

    Args:
        high: 最高价
        low: 最低价
        close: 收盘价
        period: ATR 窗口

    Returns:
        ATR Series
    """
    prev_close = close.shift(1)

    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    return tr.rolling(window=period).mean()


def latest_vol_scores(
    prices: dict[str, pd.Series],
    window: int | None = None,
    settings_override: Settings | None = None,
) -> pd.DataFrame:
    """计算所有 ETF 最新波动率得分（用于日报/建议）。

    Returns:
        DataFrame, index=ticker, columns=hv_20d, vol_score (0-1 归一化，值越高越危险)
    """
    settings = settings_override or get_settings()
    if window is None:
        window = settings.volatility.window

    records: list[dict] = []
    for ticker, series in prices.items():
        if series.empty:
            continue
        hv = historical_volatility(series, window=window)
        latest_hv = hv.iloc[-1] if not hv.empty else 0.0

        records.append({
            "ticker": ticker,
            f"hv_{window}d": latest_hv,
        })

    result = pd.DataFrame(records)
    if not result.empty:
        result = result.set_index("ticker")
    return result
