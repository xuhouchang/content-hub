"""
ETF 轮动策略核心。

接收 signals/ 层的因子输出，确定当前最优标的。
不关心仓位分配（由 position.py 处理），只输出"选哪个"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from config.settings import get_settings, Settings


@dataclass
class RotationSignal:
    """轮动信号输出。

    策略层输出此结构，供 position.py + reports 使用。
    """
    selected_tickers: list[str]          # 按照优先级排序
    all_ranked: pd.DataFrame             # 完整排名信息
    reason: str                          # 选择理由（供日报使用）
    is_defensive: bool                   # 是否触发了防守


def run_rotation(
    close_prices: dict[str, pd.Series],
    settings_override: Settings | None = None,
) -> RotationSignal:
    """执行 ETF 轮动，返回选股信号。

    流程:
    1. 从 signals/composite 获取综合评分排名
    2. 检查 top ETF 是否突破 MA60 防守线
    3. 若触发防守 -> 全现金
    4. 若未触发 -> 选择 top N

    Args:
        close_prices: dict[ticker -> Adj Close Series]
        settings_override: 配置覆盖

    Returns:
        RotationSignal
    """
    from signals.composite import composite_scores
    from .rules import should_go_defensive

    settings = settings_override or get_settings()
    top_n = settings.position.top_n

    # 1. 综合评分排名
    ranked = composite_scores(close_prices, settings_override=settings_override)

    if ranked.empty:
        return RotationSignal(
            selected_tickers=[],
            all_ranked=ranked,
            reason="无有效数据，全仓现金",
            is_defensive=True,
        )

    top_ticker = ranked.index[0]

    # 2. 防守检查
    top_close = close_prices.get(top_ticker)
    defensive = False
    if top_close is not None and not top_close.empty:
        defensive = should_go_defensive(top_ticker, top_close)

    # 3. 输出选择
    if defensive:
        selected: list[str] = []  # 全现金
        reason = (
            f"{top_ticker} 触发防守（价格≤MA{settings.position.defensive_on_break_ma}）"
        )
    else:
        selected = ranked.head(top_n).index.tolist()
        top_names = ", ".join(selected)
        score = ranked.iloc[0]["composite_score"]
        reason = (
            f"动量排名第1: {top_names} "
            f"(综合得分 {score:.4f})"
        )

    return RotationSignal(
        selected_tickers=selected,
        all_ranked=ranked,
        reason=reason,
        is_defensive=defensive,
    )
