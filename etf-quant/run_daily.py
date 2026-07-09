#!/usr/bin/env python3
"""
每日运行入口 — ETF 量化日报生成。

调用方式：
    python run_daily.py
    python run_daily.py --force-refresh   # 强制重新下载数据

流程：
    data/loader     → 获取标准化行情
    signals/composite → 计算因子得分
    strategy/rotation → ETF 轮动选择
    strategy/position → 仓位分配
    reports/daily    → 生成日报文件

设计原则：
    - 每步可独立注释/替换
    - 无状态（所有状态通过返回值传递）
    - 适合 OpenClaw cron job 直接调用
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# 确保项目根目录在 path 中
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from config.settings import get_settings, load_settings
from data.loader import load_etf_data
from signals.composite import composite_scores
from strategy.rotation import run_rotation
from strategy.position import allocate
from reports.daily import generate_daily_report
from review.signal_logger import save_signal, load_signals
from review.evaluator import evaluate_all
from review.review_report import generate_review_report
from workflow.notify import get_default_notifier
from workflow.feishu_log import log_daily


def setup_logging(level: str = "INFO") -> None:
    """配置日志格式。"""
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_dir / "etf-quant.log"),
        ],
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="港股 ETF 量化日报生成"
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="强制重新下载数据（忽略缓存）",
    )
    parser.add_argument(
        "--years",
        type=int,
        default=None,
        help="回溯年数",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="配置文件路径",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="日期 YYYY-MM-DD（默认今天）",
    )
    args = parser.parse_args()

    # 加载配置
    if args.config:
        settings = load_settings(args.config)
    else:
        settings = get_settings()

    setup_logging(settings.logging.level)
    logger = logging.getLogger(__name__)

    logger.info("=" * 50)
    logger.info("ETF 量化日报 — 开始运行")
    logger.info("=" * 50)

    # ── Step 1: 获取数据 ──────────────────────────────
    logger.info("Step 1/5: 获取行情数据")
    tickers = settings.universe.tickers
    years = args.years or max(
        max(settings.momentum.periods) / 252 + 1,
        settings.backtest.years,
    )

    data = load_etf_data(
        tickers=tickers,
        years=years,
        force_refresh=args.force_refresh,
    )

    if not data:
        logger.error("无可用数据，退出")
        return 1

    close_prices = {
        t: df["Adj Close"] for t, df in data.items()
        if "Adj Close" in df.columns
    }
    logger.info("数据加载完成: %d 个标的", len(close_prices))

    # ── Step 2: 计算因子 ──────────────────────────────
    logger.info("Step 2/5: 计算因子得分")
    scores = composite_scores(close_prices)
    logger.info("排名: %s", ", ".join(f"{t}({s:.4f})" for t, s in scores["composite_score"].items()))

    # ── Step 3: ETF 轮动 ──────────────────────────────
    logger.info("Step 3/5: ETF 轮动策略")
    rotation_signal = run_rotation(close_prices)
    logger.info("选择: %s", rotation_signal.selected_tickers or "全仓现金")
    logger.info("理由: %s", rotation_signal.reason)

    # ── Step 4: 仓位分配 ──────────────────────────────
    logger.info("Step 4/5: 仓位分配")
    position = allocate(
        rotation_signal.selected_tickers,
        rotation_signal.is_defensive,
    )
    logger.info("仓位: %s", position.summary)

    # ── Step 5: 生成日报 ──────────────────────────────
    logger.info("Step 5/6: 生成本日建议日报")
    daily_path = generate_daily_report(
        rotation_signal,
        position,
        close_prices=close_prices,
        report_date=args.date,
    )
    logger.info("日报已生成: %s", daily_path)

    # ── Step 6: 保存信号日志 ──────────────────────────
    logger.info("Step 6/6: 保存信号日志")
    save_signal(
        rotation_signal,
        position,
        close_prices=close_prices,
        signal_date=args.date,
    )

    # ── Step 7: 自动复盘历史建议 ──────────────────────
    logger.info("Step 7: 自动复盘历史建议")
    review_path = generate_review_report(
        close_prices=close_prices,
        report_date=args.date,
    )
    if review_path:
        logger.info("复盘报告已生成: %s", review_path)
    else:
        logger.info("复盘报告跳过（数据不足）")

    # ── Step 8: 写入飞书运行日志（今日预测 + 历史复盘合并） ──
    logger.info("Step 8: 更新飞书运行日志")
    signal_logs = load_signals(limit=50)
    evaluations = evaluate_all(signal_logs, close_prices) if signal_logs else None

    log_daily(
        rotation_signal, position,
        report_date=args.date,
        evaluations=evaluations,
        signal_logs=signal_logs,
        close_prices=close_prices,
    )

    # 通知
    notifier = get_default_notifier()
    notifier.send_message(
        "ETF 量化日报",
        f"今日建议: {position.summary}",
    )

    logger.info("=" * 50)
    logger.info("日报运行完成")
    logger.info("=" * 50)

    return 0


if __name__ == "__main__":
    sys.exit(main())
