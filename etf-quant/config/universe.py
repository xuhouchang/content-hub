"""
ETF 标的元信息定义。

所有标的信息在此集中管理：
- ticker -> 名称/类型/防守标记映射
- data/strategy/reports 各层从 universe 查询标的属性，不各自定义
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar


@dataclass(frozen=True)
class ETFInfo:
    """单个 ETF 的静态元信息。"""
    ticker: str
    name: str
    asset_type: str  # "equity" | "defensive"
    is_defensive: bool = False


class Universe:
    """标的池定义，所有模块从此处查标的属性。

    设计原则：
    - 全量信息在 universe 集中管理
    - strategy/reports 从 universe 查 is_defensive，不自己硬编码
    - 可映射 ticker 到 display name
    """

    # ── 标的全量列表 ──────────────────────────────────────────
    ALL: ClassVar[list[ETFInfo]] = [
        ETFInfo("02800.HK", "恒生指数 ETF", "equity"),
        ETFInfo("03033.HK", "恒生科技 ETF", "equity"),
        ETFInfo("02828.HK", "国企 ETF", "equity"),
        ETFInfo("02840.HK", "SPDR 金 ETF", "defensive", is_defensive=True),
    ]

    # ── 便捷查找 ──────────────────────────────────────────────
    _TICKER_MAP: ClassVar[dict[str, ETFInfo]] = {e.ticker: e for e in ALL}

    @classmethod
    def get(cls, ticker: str) -> ETFInfo | None:
        """获取单个 ETF 元信息，不存在返回 None。"""
        return cls._TICKER_MAP.get(ticker)

    @classmethod
    def defensive_tickers(cls) -> list[str]:
        """返回所有防守资产 ticker 列表（含 CASH 占位）。"""
        return [e.ticker for e in cls.ALL if e.is_defensive] + ["CASH"]

    @classmethod
    def equity_tickers(cls) -> list[str]:
        """返回所有权益类 ETF ticker 列表。"""
        return [e.ticker for e in cls.ALL if not e.is_defensive]

    @classmethod
    def is_defensive(cls, ticker: str) -> bool:
        """判断某个 ticker 是否为防守资产。"""
        if ticker == "CASH":
            return True
        info = cls.get(ticker)
        return info.is_defensive if info else False

    @classmethod
    def display_name(cls, ticker: str) -> str:
        """获取 ticker 对应的显示名称。"""
        if ticker == "CASH":
            return "现金"
        info = cls.get(ticker)
        return info.name if info else ticker

    @classmethod
    def all_tickers(cls) -> list[str]:
        """返回所有 ticker（不含 CASH，仅下跌时 tradeable 标的）。"""
        return [e.ticker for e in cls.ALL]
