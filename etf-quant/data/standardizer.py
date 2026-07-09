"""
数据标准化。

职责：
- 统一来自不同数据源的 DataFrame 格式
- 对齐索引时区
- 填充缺失交易日（港股休市处理）
"""

from __future__ import annotations

import logging

import pandas as pd

logger = logging.getLogger(__name__)


# ── 标准化后列定义 ──────────────────────────────────────
STANDARD_COLUMNS = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]

# OHLCV 数值列（Volume 不属于此处）
OHLC_COLUMNS = ["Open", "High", "Low", "Close", "Adj Close"]


def standardize(
    raw_data: dict[str, pd.DataFrame],
) -> dict[str, pd.DataFrame]:
    """将多个 ETF 的原始数据统一标准化。

    标准化操作：
    1. 确保列名为 STANDARD_COLUMNS 格式
    2. 确保索引为 UTC DatetimeIndex
    3. 去除全 NaN 行和重复索引
    4. 按日期升序排列
    """
    result: dict[str, pd.DataFrame] = {}

    for ticker, df in raw_data.items():
        df = df.copy()

        # 确保 DatetimeIndex
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index)

        # 统一时区到 Naive（避免 yfinance 有时区）
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)

        # 去重、排序
        df = df[~df.index.duplicated(keep="last")]
        df = df.sort_index()

        # 只保留标准列
        available = [c for c in STANDARD_COLUMNS if c in df.columns]
        df = df[available]

        # 去除全 NaN 行
        df = df.dropna(how="all")

        result[ticker] = df

    return result


def align_timestamps(
    data: dict[str, pd.DataFrame],
) -> dict[str, pd.DataFrame]:
    """对齐多标的交易日序列。

    港股所有 ETF 在同一市场交易，交易日应该一致。
    此函数只做检查，不做填充，避免引入 future bias。
    """
    if not data:
        return data

    # 检查各标的日期范围差异
    dates = {t: df.index for t, df in data.items()}
    all_dates = pd.DatetimeIndex(
        sorted(set().union(*[set(d) for d in dates.values()]))
    )

    for ticker, df in data.items():
        missing = all_dates.difference(df.index)
        if len(missing) > 0:
            logger.info(
                "%s 缺失 %d 个交易日（共 %d 天）",
                ticker, len(missing), len(all_dates),
            )

    return data
