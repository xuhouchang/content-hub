"""
数据源抽象接口。

所有数据源（Yahoo / Stooq / Alpha Vantage 等）实现此接口。
data/loader.py 通过此接口调用，不依赖具体数据源实现。

fallback 方案：
    sources = FailoverSource([YahooSource(), StooqSource()])
    data = sources.fetch(tickers, start, end)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

import pandas as pd


@dataclass
class FetchResult:
    """统一的数据获取返回结构。

    data[ticker] -> pd.DataFrame, 标准列: Open, High, Low, Close, Adj Close, Volume
    """
    data: dict[str, pd.DataFrame]
    source_name: str
    fetched_at: datetime
    errors: dict[str, str]  # ticker -> error_message


class DataSource(ABC):
    """数据源抽象基类。"""

    @property
    @abstractmethod
    def name(self) -> str:
        """数据源名称，用于日志/缓存标记。"""
        ...

    @abstractmethod
    def fetch(
        self,
        tickers: list[str],
        start: str,
        end: str | None = None,
    ) -> FetchResult:
        """获取历史行情数据。

        Args:
            tickers: 标的代码列表
            start: 开始日期 "YYYY-MM-DD"
            end: 结束日期，None 表示最新

        Returns:
            FetchResult 包含所有成功和失败的数据
        """
        ...

    def validate_data(self, df: pd.DataFrame) -> pd.DataFrame:
        """数据质量基本校验。

        - 确保必要列存在
        - 确保索引为 DatetimeIndex
        - 去除全 NaN 行
        """
        required = ["Open", "High", "Low", "Close", "Volume"]
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise ValueError(f"数据缺少必要列: {missing}")

        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index)

        return df.dropna(how="all")


class FailoverSource(DataSource):
    """多数据源容错包装器。

    按顺序尝试每个数据源，全部失败才抛出异常。
    """

    def __init__(self, sources: list[DataSource]) -> None:
        if not sources:
            raise ValueError("至少需要一个数据源")
        self._sources = sources

    @property
    def name(self) -> str:
        return f"failover({','.join(s.name for s in self._sources)})"

    def fetch(
        self,
        tickers: list[str],
        start: str,
        end: str | None = None,
    ) -> FetchResult:
        last_error: Exception | None = None
        for source in self._sources:
            try:
                return source.fetch(tickers, start, end)
            except Exception as e:
                last_error = e
                continue

        raise RuntimeError(
            f"所有数据源均失败 (tickers={tickers})"
        ) from last_error
