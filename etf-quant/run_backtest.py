#!/usr/bin/env python3
"""
回测执行入口。

调用方式：
    python run_backtest.py
    python run_backtest.py --years 2        # 回测 2 年
    python run_backtest.py --freq W         # 周频调仓

设计原则：
    - 复用 strategy/ 模块（与 run_daily.py 同一份策略代码）
    - 只输出指标和图表，不修改 strategy/ 逻辑
    - 适合策略迭代时快速验证
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from config.settings import get_settings
from data.loader import load_etf_data
from backtest.quick import run_quick_backtest


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="港股 ETF 回测"
    )
    parser.add_argument(
        "--years",
        type=int,
        default=None,
        help="回测年数（默认 config 中的值）",
    )
    parser.add_argument(
        "--freq",
        type=str,
        default=None,
        help="调仓频率 (D=日, W=周, 2W=双周, M=月)",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="强制重新下载数据",
    )
    args = parser.parse_args()

    setup_logging()
    logger = logging.getLogger(__name__)

    settings = get_settings()

    logger.info("=" * 50)
    logger.info("ETF 量化回测 — 开始")
    logger.info("=" * 50)

    years = args.years or settings.backtest.years
    freq = args.freq or settings.backtest.rebalance_freq

    logger.info("参数: years=%d, freq=%s", years, freq)

    # 获取数据
    logger.info("加载数据...")
    data = load_etf_data(
        years=years,
        force_refresh=args.force_refresh,
    )

    close_prices = {
        t: df["Adj Close"] for t, df in data.items()
        if "Adj Close" in df.columns
    }

    if not close_prices:
        logger.error("无可用数据")
        return 1

    logger.info("数据加载完成 (%d 标的, %d 交易日)", len(close_prices), len(next(iter(close_prices.values()))))

    # 运行回测
    logger.info("运行回测 (调仓频率: %s)...", freq)
    result = run_quick_backtest(close_prices)

    if not result:
        logger.error("回测未产生结果")
        return 1

    metrics = result.get("metrics", {})
    print("\n")

    # 输出详细指标
    summary = (
        "回测结果摘要:\n"
        f"  年化收益率:   {metrics.get('annual_return_pct', 0):>7.2f}%\n"
        f"  年化波动率:   {metrics.get('annual_vol_pct', 0):>7.2f}%\n"
        f"  夏普比率:     {metrics.get('sharpe_ratio', 0):>7.4f}\n"
        f"  最大回撤:     {metrics.get('max_drawdown_pct', 0):>7.2f}%\n"
        f"  Calmar 比率:  {metrics.get('calmar_ratio', 0):>7.4f}\n"
        f"  胜率:         {metrics.get('win_rate_pct', 0):>7.2f}%\n"
        f"  基准收益率:   {metrics.get('benchmark_return_pct', 0):>7.2f}%\n"
        f"  超额收益:     {metrics.get('excess_return_pct', 0):>7.2f}%\n"
    )
    print(summary)

    logger.info("=" * 50)
    logger.info("回测完成")
    logger.info("=" * 50)

    return 0


if __name__ == "__main__":
    sys.exit(main())
