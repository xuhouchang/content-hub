"""
数据获取统一入口。

职责：
- 对外提供 load_etf_data() 接口
- 内部协调：缓存优先 -> 增量下载 -> 标准化
- 所有 data/ 模块外部调用方只 import loader

使用方式：
    from data.loader import load_etf_data
    data = load_etf_data(tickers=["02800.HK", "03033.HK"], years=2)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pandas as pd

from config.settings import get_settings, Settings
from .cache import load_from_cache, merge_and_update, get_cached_range
from .sources.base import DataSource
from .sources.yahoo import YahooSource
from .standardizer import standardize, align_timestamps

logger = logging.getLogger(__name__)


def _default_source() -> DataSource:
    """返回当前配置的主数据源。"""
    settings = get_settings()
    source_name = settings.data.primary_source

    if source_name == "yahoo":
        return YahooSource()
    # 后续扩展: stooq, alphavantage 等

    raise ValueError(f"未知数据源: {source_name}")


def load_etf_data(
    tickers: list[str] | None = None,
    years: int | None = None,
    start: str | None = None,
    end: str | None = None,
    source: DataSource | None = None,
    force_refresh: bool = False,
    settings_override: Settings | None = None,
) -> dict[str, pd.DataFrame]:
    """获取多个 ETF 的标准化历史数据。

    Args:
        tickers: 标的代码列表，默认从配置读取
        years: 回溯年数（与 start 不能同时指定）
        start: 开始日期 "YYYY-MM-DD"
        end: 结束日期，None 表示最新
        source: 数据源，默认从配置读取
        force_refresh: 强制重新下载（忽略缓存）
        settings_override: 配置覆盖（测试用）

    Returns:
        dict[ticker -> 标准化 DataFrame]
        列: Open, High, Low, Close, Adj Close, Volume
    """
    settings = settings_override or get_settings()

    if tickers is None:
        tickers = settings.universe.tickers

    # 排除 CASH
    fetch_tickers = [t for t in tickers if t != "CASH"]

    # 确定日期范围
    use_start, use_end = _resolve_date_range(years, start, end)
    logger.info(
        "加载 ETF 数据: %s [%s ~ %s]",
        fetch_tickers, use_start, use_end or "now",
    )

    # ── 阶段1: 从缓存读取已有数据 ──────────────────────
    cached_data: dict[str, pd.DataFrame] = {}
    if not force_refresh:
        for ticker in fetch_tickers:
            df = load_from_cache(ticker)
            if df is not None and not df.empty:
                cached_data[ticker] = df

    # ── 阶段2: 确定需要增量下载的日期范围 ──────────────
    need_fetch: list[str] = []
    min_start = pd.Timestamp(use_start)

    for ticker in fetch_tickers:
        if ticker in cached_data:
            cache_min, cache_max = get_cached_range(ticker)
            if cache_min is not None and cache_min <= min_start:
                # 缓存数据足够
                if end is None or (cache_max is not None and cache_max >= pd.Timestamp(end)):
                    logger.info("缓存已覆盖 %s, 跳过下载", ticker)
                    continue
        need_fetch.append(ticker)

    # ── 阶段3: 增量下载 ────────────────────────────────
    if need_fetch:
        actual_source = source or _default_source()
        result = actual_source.fetch(need_fetch, use_start, use_end)

        if result.errors:
            logger.warning("部分数据获取失败: %s", result.errors)

        # 合并到缓存
        for ticker, df in result.data.items():
            combined = merge_and_update(ticker, df)
            cached_data[ticker] = combined

    # ── 阶段4: 标准化 ──────────────────────────────────
    std_data = standardize(cached_data)
    std_data = align_timestamps(std_data)

    # ── 阶段5: 裁剪到请求的时间范围 ────────────────────
    result: dict[str, pd.DataFrame] = {}
    for ticker, df in std_data.items():
        mask = df.index >= min_start
        if use_end:
            mask &= df.index <= pd.Timestamp(use_end)
        result[ticker] = df[mask]

    return result


def _resolve_date_range(
    years: int | None = None,
    start: str | None = None,
    end: str | None = None,
) -> tuple[str, str | None]:
    """确定下载日期范围。"""
    if start is not None:
        return start, end

    if years is None:
        years = 1  # 默认1年

    dt_end = pd.Timestamp(end) if end else pd.Timestamp.now()
    dt_start = dt_end - timedelta(days=int(years * 365 * 1.2))  # 多取20%确保数据充足

    return dt_start.strftime("%Y-%m-%d"), end
