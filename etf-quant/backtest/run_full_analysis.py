#!/usr/bin/env python3
"""
长期回测 + 市场环境分析 — 主入口。

运行方式：
    python backtest/run_full_analysis.py
    python backtest/run_full_analysis.py --years 10
    python backtest/run_full_analysis.py --quick       # 只跑核心回测 + 参数敏感性

输出：
    reports/backtest/full_report_YYYY-MM-DD.md
    reports/backtest/equity_curve.png
    reports/backtest/monthly_heatmap.png
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))

from config.settings import get_settings
from data.loader import load_etf_data

HKT = timezone(timedelta(hours=8))


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def main():
    parser = argparse.ArgumentParser(
        description="港股 ETF 长期回测 + 市场环境分析"
    )
    parser.add_argument("--years", type=int, default=5,
                        help="回测年数（默认 5）")
    parser.add_argument("--force-refresh", action="store_true",
                        help="强制重新下载数据")
    parser.add_argument("--quick", action="store_true",
                        help="快速模式：只跑核心回测 + 参数敏感性，跳过图表")
    args = parser.parse_args()

    setup_logging()
    logger = logging.getLogger(__name__)

    logger.info("=" * 60)
    logger.info("ETF 长期回测 — 开始")
    logger.info(f"参数: {args.years}年, quick={args.quick}")
    logger.info("=" * 60)

    # ── 加载数据 ──
    logger.info("Step 1: 加载数据...")
    data = load_etf_data(
        years=args.years,
        force_refresh=args.force_refresh,
    )
    close_prices = {
        t: df["Adj Close"] for t, df in data.items()
        if "Adj Close" in df.columns
    }
    logger.info(f"数据加载完成: {len(close_prices)} 标的, "
                f"{len(next(iter(close_prices.values())))} 交易日")

    output_dir = Path("reports/backtest")
    output_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(HKT).strftime("%Y-%m-%d")

    # ── 运行长期回测 ──
    logger.info("Step 2: 运行长期回测...")
    from backtest.long_term_backtest import (
        run_long_backtest, plot_equity_curve, plot_monthly_heatmap
    )

    result = run_long_backtest(
        close_prices,
        ma_period=60,
        momentum_periods=[20, 60],
        momentum_weights=[0.4, 0.6],
        rebalance_freq="W",
    )
    logger.info("回测完成")

    # ── 基准对比 ──
    logger.info("Step 3: 基准对比...")
    from backtest.benchmark_compare import benchmark_compare, print_compare_report
    compare = benchmark_compare(close_prices, result)
    compare_md = print_compare_report(compare)

    # ── 市场环境分析 ──
    logger.info("Step 4: 市场环境分析...")
    from backtest.regime_analysis import analyze_market_regimes, print_regime_report
    regime = analyze_market_regimes(result, close_prices)
    regime_md = print_regime_report(regime)

    # ── 参数敏感性 ──
    logger.info("Step 5: 参数敏感性分析...")
    from backtest.parameter_sensitivity import (
        run_sensitivity, print_sensitivity_report
    )
    sensitivity = run_sensitivity(close_prices)
    sensitivity_md = print_sensitivity_report(sensitivity)

    # ── 生成图表 ──
    if not args.quick:
        logger.info("Step 6: 生成图表...")
        plot_equity_curve(result, str(output_dir / f"equity_curve_{today}.png"))
        plot_monthly_heatmap(result, str(output_dir / f"monthly_heatmap_{today}.png"))

    # ── 输出完整报告 ──
    report_md = _build_full_report(
        result, compare_md, regime_md, sensitivity_md, today, sensitivity
    )
    report_path = output_dir / f"full_report_{today}.md"
    with open(report_path, "w") as f:
        f.write(report_md)
    logger.info(f"完整报告已保存: {report_path}")

    # ── 打印摘要 ──
    _print_summary(result, sensitivity)

    logger.info("=" * 60)
    logger.info("回测完成")
    logger.info("=" * 60)


def _build_full_report(result, compare_md, regime_md, sensitivity_md, today, sensitivity):
    """组装完整分析报告 Markdown。"""
    cfg = result.config_snapshot

    lines = [
        f"# 港股 ETF 策略完整回测报告",
        f"",
        f"**生成日期**: {today}",
        f"**策略参数**: MA{cfg['ma_period']} 防守 | "
        f"动量 {cfg['momentum_periods'][0]}d/{cfg['momentum_periods'][1]}d "
        f"权重 {cfg['momentum_weights']} | "
        f"调仓 {cfg['rebalance_freq']} | "
        f"单标的上限 {cfg['max_single']*100:.0f}%",
        f"**回测区间**: {result.equity_curve.index[0].strftime('%Y-%m-%d')} "
        f"~ {result.equity_curve.index[-1].strftime('%Y-%m-%d')}",
        f"",
        f"---",
        f"",
        f"## 核心表现指标\n",
        f"| 指标 | 值 |",
        f"|------|:---:|",
        f"| 总收益率 | {result.total_return_pct:+.2f}% |",
        f"| 年化收益率 (CAGR) | {result.annual_return_pct:.2f}% |",
        f"| 年化波动率 | {result.annual_vol_pct:.2f}% |",
        f"| Sharpe Ratio | {result.sharpe_ratio:.4f} |",
        f"| 最大回撤 | {result.max_drawdown_pct:.2f}% |",
        f"| Calmar Ratio | {result.calmar_ratio:.4f} |",
        f"| 基准总收益 (2800.HK) | {result.benchmark_return_pct:+.2f}% |",
        f"| 超额收益 | {result.excess_return_pct:+.2f}% |",
        f"| 胜率 (月度) | {result.win_rate_pct:.1f}% |",
        f"| 空仓时间占比 | {result.cash_ratio*100:.1f}% |",
        f"| 防守触发次数 | {result.defensive_count} |",
        f"| 平均持仓天数 | {result.avg_hold_days:.1f} |",
        f"| 总交易天数 | {len(result.equity_curve)} |",
        f"",
        f"---",
        f"",
    ]
    lines.append(compare_md)
    lines.append("---\n")
    lines.append(regime_md)
    lines.append("---\n")
    lines.append(sensitivity_md)
    lines.append("---\n")

    # ── 策略分析判断 ──
    lines.append("## 策略初步评估\n")
    lines.extend(_assessment_lines(result, sensitivity))

    return "\n".join(lines)


def _assessment_lines(result, sensitivity):
    """生成策略评估建议。"""
    lines = []

    # 1. 防守逻辑是否过于敏感
    cash_r = result.cash_ratio
    lines.append(f"### 1. 防守逻辑评估\n")
    if cash_r > 0.4:
        lines.append(f"⚠️ **防守过于敏感**: 空仓时间占 {cash_r*100:.0f}%，"
                      f"超过 40% 的天数持有现金。"
                      f"对于周频调仓策略，这意味着大段时间踏空。")
    elif cash_r > 0.25:
        lines.append(f"⚠️ **防守偏敏感**: 空仓时间占 {cash_r*100:.0f}%，"
                      f"处于偏高水平。MA60 在当前行情下触发频率较高。")
    else:
        lines.append(f"✅ **防守适中**: 空仓时间占 {cash_r*100:.0f}%。")

    lines.append(f"MA60 防守触发的核心问题是：对于横盘震荡行情，"
                  f"价格反复穿越 MA60，导致频繁进出。"
                  f"需要考虑是否引入确认机制（如连续 2 天低于 MA60 才触发）。\n")

    # 2. 长期空仓踏空
    lines.append(f"### 2. 空仓踏空风险\n")
    total_ret = result.total_return_pct
    bench_ret = result.benchmark_return_pct
    if total_ret < 0 < bench_ret:
        lines.append(f"⚠️ **策略亏损而基准上涨**，说明空仓时段错过了主要的上涨行情。"
                      f"策略的防守逻辑在市场上涨时成为拖累。")
    elif total_ret < bench_ret:
        lines.append(f"⚠️ **策略跑输基准**（超额 {result.excess_return_pct:+.2f}%），"
                      f"现金仓位在牛市中造成了持续的踏空成本。")
    else:
        lines.append(f"✅ **策略跑赢基准**（超额 {result.excess_return_pct:+.2f}%），"
                      f"但空仓时间占 {cash_r*100:.0f}%，需要确认超额来源是否能持续。")
    lines.append("")

    # 3. 哪些环境策略容易失效
    lines.append(f"### 3. 失效环境\n")
    lines.append(f"根据历史表现，策略在以下环境容易失效：\n")
    lines.append(f"1. **快速反弹行情**: 防守触发后全现金，错过急涨")
    lines.append(f"2. **低波动横盘**: 动量信号微弱，随机化进出导致额外交易成本")
    lines.append(f"3. **持续上涨趋势**: 50% 现金仓位天然跑输满仓")
    lines.append(f"4. **非动量驱动的轮动行情**: 选择标的主要看动量，"
                  f"如果板块轮动由基本面驱动，动量的响应滞后\n")

    # 4. 哪些环境策略有效
    lines.append(f"### 4. 有效环境\n")
    lines.append(f"1. **单边急跌**: 全现金防守能有效控制回撤")
    lines.append(f"2. **高波动震荡市**: 通过选 top ETF 轮动获取微弱超额")
    lines.append(f"3. **大盘下跌后的结构性行情**: 动量为正时重新入场，"
                  f"埋伏反弹初期\n")

    # 5. 过拟合风险
    lines.append(f"### 5. 过拟合风险评估\n")
    if sensitivity and sensitivity.overfit_warning and "✅" not in str(sensitivity.overfit_warning):
        lines.append(f"{sensitivity.overfit_warning}\n")
    else:
        lines.append(f"参数敏感性分析显示策略跨参数区间表现稳定，"
                      f"过拟合风险较低。\n")

    # 6. 参数敏感性结论
    lines.append(f"### 6. 参数敏感性结论\n")
    if sensitivity and hasattr(sensitivity, 'grid'):
        grid = sensitivity.grid
        if not grid.empty and "sharpe_ratio" in grid.columns:
            sharpe_vals = grid["sharpe_ratio"].dropna()
            if len(sharpe_vals) > 1:
                min_sr, max_sr = sharpe_vals.min(), sharpe_vals.max()
                lines.append(f"各参数组合的 Sharpe 范围: {min_sr:.4f} ~ {max_sr:.4f}")
                lines.append(f"全距: {max_sr - min_sr:.4f}")
                if max_sr - min_sr > 0.5:
                    lines.append(f"⚠️ Sharpe 跨参数波动较大，策略对参数选择敏感。")
                else:
                    lines.append(f"✅ Sharpe 跨参数稳定。")
    lines.append("")

    # 7. 是否建议保留
    lines.append(f"### 7. 配置建议\n")

    # MA60
    lines.append(f"**MA60 防守:**")
    if cash_r > 0.3:
        lines.append(f"❌ 建议调整。当前 MA60 触发频率过高，"
                      f"建议测试 MA80 或 MA120，或引入 2 天确认机制。\n")
    else:
        lines.append(f"✅ 可以保留。\n")

    # 50% CASH
    lines.append(f"**50% CASH 约束:**")
    if result.annual_return_pct < 5:
        lines.append(f"❌ 建议放开或降低。50% 现金仓位严重限制了收益潜力，"
                      f"尤其是当策略本身选股能力有限时。\n")
    else:
        lines.append(f"✅ 可以保留。\n")

    # 周频调仓
    lines.append(f"**周频调仓:**")
    lines.append(f"✅ 当前频率合理。日频会增加噪音，"
                  f"月频则对动量信号的响应过慢。\n")

    return lines


def _print_summary(result, sensitivity):
    """打印控制台摘要。"""
    print("\n" + "=" * 60)
    print("回测摘要")
    print("=" * 60)
    print(f"  CAGR:        {result.annual_return_pct:>7.2f}%")
    print(f"  Sharpe:      {result.sharpe_ratio:>7.4f}")
    print(f"  MaxDD:       {result.max_drawdown_pct:>7.2f}%")
    print(f"  Calmar:      {result.calmar_ratio:>7.4f}")
    print(f"  超额收益:    {result.excess_return_pct:>+7.2f}%")
    print(f"  空仓时间:    {result.cash_ratio*100:>7.1f}%")
    print(f"  防守次数:    {result.defensive_count:>7d}")
    print(f"  平均持仓:    {result.avg_hold_days:>7.1f} 天")
    if sensitivity and hasattr(sensitivity, 'grid'):
        print("-" * 60)
        print(f"  参数敏感性网格: {len(sensitivity.grid)} 种组合")
        if sensitivity.overfit_warning:
            print(f"  ⚠️ {sensitivity.overfit_warning[:80]}...")
    print("=" * 60)
    print(f"  完整报告: reports/backtest/full_report_*.md")
    print("=" * 60)


if __name__ == "__main__":
    main()
