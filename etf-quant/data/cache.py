"""
本地缓存管理。

职责：
- 将获取到的历史数据写入 data_cache/hist/{ticker}.csv
- 读取已有缓存，避免重复下载
- 增量更新：只下载缓存中缺失的交易日
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pandas as pd

from config.settings import get_settings

logger = logging.getLogger(__name__)


def cache_path(ticker: str) -> Path:
    """返回某个 ticker 的缓存文件路径。"""
    settings = get_settings()
    cache_dir = Path(settings.data.cache_dir)
    return cache_dir / f"{ticker.replace('.', '_')}.csv"


def load_from_cache(ticker: str) -> pd.DataFrame | None:
    """从本地缓存加载数据，不存在返回 None。"""
    path = cache_path(ticker)
    if not path.exists():
        return None

    df = pd.read_csv(path, index_col=0, parse_dates=True)
    df.index.name = "Date"

    # 确保列名正确（csv 读取后可能丢失列名大小写）
    expected = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
    rename = {}
    for col in df.columns:
        col_lower = col.lower().replace(" ", "_")
        for e in expected:
            if col_lower == e.lower().replace(" ", "").replace("-", ""):
                rename[col] = e
                break
    if rename:
        df = df.rename(columns=rename)

    logger.info("从缓存加载 %s: %d 行", ticker, len(df))
    return df


def save_to_cache(ticker: str, df: pd.DataFrame) -> None:
    """写入本地缓存。"""
    path = cache_path(ticker)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path)
    logger.info("缓存写入 %s: %d 行", ticker, len(df))


def get_cached_range(ticker: str) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    """获取缓存数据的日期范围，无缓存返回 (None, None)。"""
    df = load_from_cache(ticker)
    if df is None or df.empty:
        return None, None
    return df.index.min(), df.index.max()


def merge_and_update(
    ticker: str,
    new_data: pd.DataFrame,
) -> pd.DataFrame:
    """合并新数据到缓存，去重后写入。

    Args:
        ticker: 标的代码
        new_data: 新获取的数据

    Returns:
        合并后的完整数据
    """
    existing = load_from_cache(ticker)

    if existing is not None:
        # 合并去重：新数据覆盖旧的
        combined = pd.concat([existing, new_data])
        combined = combined[~combined.index.duplicated(keep="last")]
        combined = combined.sort_index()
    else:
        combined = new_data.sort_index()

    save_to_cache(ticker, combined)
    return combined
