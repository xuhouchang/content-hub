"""
仓位管理。

职责：
- 接收 RotationSignal（选什么），输出仓位分配（买多少）
- 执行仓位约束：单标的不超过 max_single，剩余放现金
- 防守状态下全仓现金

第一版仓位规则（PLAN.md 3.1 节）：
  - top ETF 高于 MA60: top1 50%, cash 50%
  - top ETF 低于或等于 MA60: cash 100%
"""

from __future__ import annotations

from dataclasses import dataclass, field

from config.settings import get_settings


@dataclass
class PositionAllocation:
    """仓位分配结果。"""
    allocations: dict[str, float]   # ticker -> weight (0~1)
    cash_weight: float = 0.0        # 现金部分
    summary: str = ""               # 简短描述


def allocate(
    selected_tickers: list[str],
    is_defensive: bool,
    max_weight: float | None = None,
) -> PositionAllocation:
    """根据轮动信号分配仓位。

    第一版规则:
    - 防守状态: cash 100%
    - 正常状态: top1 50%, cash 50%
      （如 top_n = 2: top1 50%, top2 30%, cash 20% — 增强版预留）

    Args:
        selected_tickers: 按优先级排序的选中标的
        is_defensive: 是否触发了防守
        max_weight: 单标的仓位上限，默认从配置读取

    Returns:
        PositionAllocation
    """
    settings = get_settings()
    if max_weight is None:
        max_weight = settings.position.max_single

    if is_defensive or not selected_tickers:
        return PositionAllocation(
            allocations={},
            cash_weight=1.0,
            summary="全仓现金（防守）",
        )

    # 第一版: 均匀分配 + 现金兜底
    # 只有 1 个标的时: 50% ETF + 50% cash
    n = len(selected_tickers)
    per_etf_weight = min(max_weight, 0.8 / n)
    used = per_etf_weight * n
    cash = 1.0 - used

    allocations = {t: per_etf_weight for t in selected_tickers}

    summary_parts = [f"{t}: {w*100:.0f}%" for t, w in allocations.items()]
    summary_parts.append(f"现金: {cash*100:.0f}%")

    return PositionAllocation(
        allocations=allocations,
        cash_weight=cash,
        summary=" | ".join(summary_parts),
    )
