"""
综合因子得分。

将动量、趋势、波动率信号组合成统一得分。
所有配置参数来自 config.yaml，不在代码中硬编码。
"""

from __future__ import annotations

import pandas as pd

from config.settings import get_settings, Settings
from .momentum import latest_momentum_scores
from .volatility import latest_vol_scores


def composite_scores(
    close_prices: dict[str, pd.Series],
    penalty_coeff: float | None = None,
    settings_override: Settings | None = None,
) -> pd.DataFrame:
    """计算综合因子得分。

    score = w1 * ret_20d + w2 * ret_60d - coeff * vol_20d

    Args:
        close_prices: dict[ticker -> Adj Close Series]
        penalty_coeff: 波动率惩罚系数，默认从配置读取
        settings_override: 配置覆盖（测试/回测用）

    Returns:
        DataFrame, 列: ticker, ret_20, ret_60, momentum_score, hv_20d, composite_score, rank
    """
    settings = settings_override or get_settings()

    if penalty_coeff is None:
        penalty_coeff = settings.volatility.penalty

    # 动量得分
    mom = latest_momentum_scores(close_prices, settings_override=settings_override)

    # 波动率得分
    vol = latest_vol_scores(close_prices, settings_override=settings_override)

    # 合并
    combined = mom.join(vol, how="left")

    hv_col = f"hv_{settings.volatility.window}d"
    if hv_col not in combined.columns:
        combined[hv_col] = 0.0

    # 计算综合得分
    combined["composite_score"] = (
        combined["momentum_score"]
        - penalty_coeff * combined[hv_col]
    )

    combined = combined.sort_values("composite_score", ascending=False)
    combined["rank"] = range(1, len(combined) + 1)

    return combined


def top_n_by_score(
    close_prices: dict[str, pd.Series],
    n: int = 1,
) -> list[str]:
    """返回得分最高的 N 个 ETF ticker。

    Args:
        close_prices: dict[ticker -> Adj Close Series]
        n: 返回数量

    Returns:
        排名前 N 的 ticker 列表
    """
    scores = composite_scores(close_prices)
    if scores.empty:
        return []
    return scores.head(n).index.tolist()
