"""
长期历史回测 — 5~10 年，完整策略复刻，详细指标。

设计原则：
- 与 run_daily.py 共用同一份 strategy/ 信号逻辑（纯函数）
- 不修改实盘策略代码
- 输出 Equity Curve, Drawdown Curve, Monthly Heatmap 图表
- 支持配置覆盖（测试不同参数）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

HKT = timezone(timedelta(hours=8))


@dataclass
class LongTermResult:
    """长期回测结果。"""
    equity_curve: pd.Series
    benchmark_curve: pd.Series
    cash_days: pd.DatetimeIndex                     # 全现金持仓的交易日
    defensive_calls: list[datetime]                  # 触发防守的日期
    rebalance_dates: list[datetime]                  # 实际调仓日期
    selected_tickers_by_date: dict[str, str]         # date -> selected ticker
    signals_history: list[dict]                      # 每条调仓信号的明细
    price_data: pd.DataFrame                         # 对齐后的价格数据（用于后续分析）
    annual_returns: pd.Series                        # 年度收益率
    monthly_returns: pd.Series                       # 月度收益率
    regime_labels: pd.Series | None = None           # 市场状态标签（由regime_analysis写入）
    config_snapshot: dict = field(default_factory=dict)

    @property
    def total_return_pct(self) -> float:
        return float((self.equity_curve.iloc[-1] / self.equity_curve.iloc[0] - 1) * 100)

    @property
    def benchmark_return_pct(self) -> float:
        return float((self.benchmark_curve.iloc[-1] / self.benchmark_curve.iloc[0] - 1) * 100)

    @property
    def excess_return_pct(self) -> float:
        return self.total_return_pct - self.benchmark_return_pct

    @property
    def annual_return_pct(self) -> float:
        return _annualized_return(self.equity_curve) * 100

    @property
    def annual_vol_pct(self) -> float:
        return _annualized_vol(self.equity_curve) * 100

    @property
    def sharpe_ratio(self) -> float:
        ret = _annualized_return(self.equity_curve)
        vol = _annualized_vol(self.equity_curve)
        if vol == 0:
            return 0.0
        return (ret - 0.03) / vol

    @property
    def max_drawdown_pct(self) -> float:
        if len(self.equity_curve) < 2:
            return 0.0
        cummax = self.equity_curve.cummax()
        dd = (self.equity_curve - cummax) / cummax
        return float(dd.min() * 100)

    @property
    def calmar_ratio(self) -> float:
        mdd = self.max_drawdown_pct / 100
        if mdd == 0:
            return 0.0
        return self.annual_return_pct / 100 / abs(mdd)

    @property
    def cash_ratio(self) -> float:
        """空仓时间占比（全现金的交易日比例）。"""
        total = len(self.equity_curve)
        if total == 0:
            return 0.0
        return len(self.cash_days) / total

    @property
    def defensive_count(self) -> int:
        return len(self.defensive_calls)

    @property
    def avg_hold_days(self) -> float:
        """平均持仓天数（非现金状态的平均连续天数）。"""
        is_cash = pd.Series(0, index=self.equity_curve.index)
        is_cash.loc[self.cash_days] = 1

        # 找连续非现金的"run"
        runs = (is_cash == 0).astype(int).groupby(
            (is_cash != is_cash.shift()).cumsum()
        ).sum()
        non_zero = runs[runs > 0]
        if len(non_zero) == 0:
            return 0.0
        return float(non_zero.mean())

    @property
    def win_rate_pct(self) -> float:
        """月度胜率。"""
        if len(self.monthly_returns) < 2:
            return 0.0
        return float((self.monthly_returns > 0).sum() / len(self.monthly_returns) * 100)


def run_long_backtest(
    close_prices: dict[str, pd.Series],
    settings_override: Any = None,
    ma_period: int = 60,
    momentum_periods: list[int] | None = None,
    momentum_weights: list[float] | None = None,
    rebalance_freq: str = "W",
    max_single: float = 0.5,
    cash_default: float = 0.5,
) -> LongTermResult:
    """运行长期回测。

    Args:
        close_prices: dict[ticker -> Adj Close Series]
        settings_override: 配置覆盖对象（优先级高）
        ma_period: MA 防守周期
        momentum_periods: 动量窗口，如 [20, 60]
        momentum_weights: 动量权重，如 [0.4, 0.6]
        rebalance_freq: 调仓频率
        max_single: 单标的最大仓位
        cash_default: 默认现金比例

    Returns:
        LongTermResult
    """
    from config.settings import get_settings, Settings

    # ── 配置构建：复制真实配置，覆盖回测参数 ──
    base = get_settings()
    if settings_override:
        cfg = settings_override
    else:
        cfg = _make_settings(
            base, ma_period, momentum_periods or [20, 60],
            momentum_weights or [0.4, 0.6],
            rebalance_freq, max_single, cash_default,
        )

    from strategy.rotation import run_rotation
    from strategy.position import allocate

    # ── 对齐数据 ──
    aligned = _align_prices(close_prices)
    if aligned.empty:
        raise ValueError("没有对齐的数据")

    # ── 调仓日期 ──
    reb_dates = _rebalance_dates(aligned.index, rebalance_freq)

    # ── 回测循环 ──
    positions: dict[str, float] = {t: 0.0 for t in aligned.columns}
    positions["CASH"] = 1.0

    equity = pd.Series(1.0, index=aligned.index)
    selected_history: dict[str, str] = {}
    defensive_calls: list[datetime] = []
    all_rebalance_dates: list[datetime] = []
    signals_log: list[dict] = []
    cash_dates: list[datetime] = []

    prev_date = None

    for date in equity.index:
        needs_rebalance = date in reb_dates or prev_date is None

        if needs_rebalance:
            all_rebalance_dates.append(date.to_pydatetime())

            # 用过去所有数据（而不是 rolling window），策略本身是用全量序列算 MA
            window_prices = {t: aligned.loc[:date, t] for t in aligned.columns}

            if not window_prices or any(s.empty for s in window_prices.values()):
                continue

            signal = run_rotation(window_prices, cfg)
            alloc = allocate(
                signal.selected_tickers,
                signal.is_defensive,
                max_weight=cfg.position.max_single,
            )

            # 更新持仓
            for t in positions:
                positions[t] = 0.0
            for t, w in alloc.allocations.items():
                positions[t] = w
            positions["CASH"] = alloc.cash_weight

            # 记录
            selected_etf = signal.selected_tickers[0] if signal.selected_tickers else "CASH"
            selected_history[date.strftime("%Y-%m-%d")] = selected_etf

            if signal.is_defensive:
                defensive_calls.append(date.to_pydatetime())

            signals_log.append({
                "date": date.strftime("%Y-%m-%d"),
                "selected": selected_etf,
                "defensive": signal.is_defensive,
                "reason": signal.reason,
                "weights": {**alloc.allocations, "CASH": alloc.cash_weight},
            })

        # 每日收益
        if prev_date is not None:
            daily_ret = 0.0
            for t in positions:
                if t == "CASH" or positions[t] == 0:
                    continue
                prev_px = aligned.loc[prev_date, t]
                curr_px = aligned.loc[date, t]
                if prev_px > 0 and curr_px > 0:
                    daily_ret += positions[t] * (curr_px / prev_px - 1)
            equity.iloc[equity.index.get_loc(date)] = equity.loc[prev_date] * (1 + daily_ret)

        # 记录现金日
        if positions.get("CASH", 0) >= 1.0:
            cash_dates.append(date.to_pydatetime())

        prev_date = date

    # ── 基准曲线 ──
    benchmark_ticker = cfg.universe.tickers[0]
    benchmark_series = aligned[benchmark_ticker]
    benchmark_curve = benchmark_series / benchmark_series.iloc[0]

    # ── 年度/月度收益率 ──
    equity_series = pd.Series(equity, index=aligned.index)
    rets = equity_series.pct_change().dropna()

    annual_ret = rets.groupby(rets.index.year).apply(
        lambda x: (1 + x).prod() - 1
    )
    monthly_ret = rets.groupby(rets.index.to_period("M")).apply(
        lambda x: (1 + x).prod() - 1
    )

    return LongTermResult(
        equity_curve=equity_series,
        benchmark_curve=benchmark_curve,
        cash_days=pd.DatetimeIndex(cash_dates),
        defensive_calls=defensive_calls,
        rebalance_dates=all_rebalance_dates,
        selected_tickers_by_date=selected_history,
        signals_history=signals_log,
        price_data=aligned,
        annual_returns=annual_ret,
        monthly_returns=monthly_ret,
        config_snapshot={
            "ma_period": ma_period,
            "momentum_periods": momentum_periods or [20, 60],
            "momentum_weights": momentum_weights or [0.4, 0.6],
            "rebalance_freq": rebalance_freq,
            "max_single": max_single,
            "cash_default": cash_default,
        },
    )


# ═══════════════════════════════════════════════════════
#  辅助函数
# ═══════════════════════════════════════════════════════

def _align_prices(prices: dict[str, pd.Series]) -> pd.DataFrame:
    """对齐价格序列为 DataFrame，填充缺失值。"""
    df = pd.DataFrame(prices)
    # 前向填充（港股休市时）
    df = df.ffill()
    # 丢弃全空行
    df = df.dropna(how="all")
    return df


def _rebalance_dates(dates: pd.DatetimeIndex, freq: str) -> set:
    """生成调仓日期集合。
    
    支持频率: D=每日, W=每周, 2W=双周, M=每月。
    不用 groupby 避免 NaN dropna 问题，直接用 isocalendar 识别周期首日。
    """
    if freq.upper() in ("D", "DAY"):
        return set(dates)

    # 按 year-week 识别每周第一个交易日
    if freq.upper() in ("W", "WEEK", "1W"):
        result = []
        prev_key = None
        for d in dates:
            iso = d.isocalendar()
            key = (iso.year, iso.week)
            if key != prev_key:
                result.append(d)
                prev_key = key
        return set(result)

    if freq.upper() in ("2W", "BIWEEK", "BI-WEEK"):
        result = []
        prev_key = None
        for d in dates:
            iso = d.isocalendar()
            key = (iso.year, iso.week // 2)
            if key != prev_key:
                result.append(d)
                prev_key = key
        return set(result)

    if freq.upper() in ("M", "MONTH"):
        result = []
        prev_key = None
        for d in dates:
            key = (d.year, d.month)
            if key != prev_key:
                result.append(d)
                prev_key = key
        return set(result)

    # fallback
    return set(dates)


def _make_settings(base, ma_period, mom_periods, mom_weights, freq, max_single, cash_default):
    """复制基准配置 + 覆盖回测参数。
    
    保持大部分字段指向 base 的原有值，只覆盖策略关键参数。
    """
    from types import SimpleNamespace

    cfg = SimpleNamespace()

    # position — 覆盖
    cfg.position = SimpleNamespace(
        defensive_on_break_ma=ma_period,
        max_single=max_single,
        cash_default=cash_default,
        top_n=base.position.top_n,
        stop_loss=base.position.stop_loss if hasattr(base.position, 'stop_loss') else None,
        trailing_stop=base.position.trailing_stop if hasattr(base.position, 'trailing_stop') else None,
    )

    # momentum — 覆盖
    cfg.momentum = SimpleNamespace(
        periods=mom_periods,
        weights=mom_weights,
    )

    # volatility — 从 base 复制，只覆盖窗口
    cfg.volatility = SimpleNamespace(
        window=20,
        penalty=getattr(base.volatility, 'penalty', 0.2),
        annualization_factor=getattr(base.volatility, 'annualization_factor', 252),
    )

    # trend / backtest — 覆盖
    cfg.trend = SimpleNamespace(ma_periods=[20, 60])
    cfg.backtest = SimpleNamespace(
        rebalance_freq=freq,
        years=5,
        initial_capital=1_000_000,
    )

    # 以下模块直接引用 base
    cfg.universe = base.universe
    cfg.data = base.data
    cfg.logging = base.logging
    cfg.report = base.report

    return cfg


def _annualized_return(equity: pd.Series, trading_days: int = 252) -> float:
    if len(equity) < 2:
        return 0.0
    tr = equity.iloc[-1] / equity.iloc[0]
    years = len(equity) / trading_days
    if years <= 0:
        return 0.0
    return float(tr ** (1.0 / years) - 1.0)


def _annualized_vol(equity: pd.Series, trading_days: int = 252) -> float:
    rets = equity.pct_change().dropna()
    if len(rets) < 2:
        return 0.0
    return float(rets.std() * np.sqrt(trading_days))


# ═══════════════════════════════════════════════════════
#  图表输出
# ═══════════════════════════════════════════════════════

def plot_equity_curve(result: LongTermResult, save_path: str | None = None):
    """输出权益曲线。用文本或 matplotlib。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 10), gridspec_kw={"height_ratios": [3, 1]})

    # 权益曲线
    ax1.plot(result.equity_curve.index, result.equity_curve.values,
             label=f"Strategy ({result.annual_return_pct:.1f}% CAGR)", linewidth=2)
    ax1.plot(result.benchmark_curve.index, result.benchmark_curve.values,
             label=f"2800.HK ({result.benchmark_return_pct:.1f}% total)", linewidth=1, alpha=0.7)
    ax1.axhline(1.0, color="gray", ls="--", alpha=0.3)

    # 防守触发标记
    for d in result.defensive_calls:
        if d in result.equity_curve.index:
            val = result.equity_curve.loc[d]
            ax1.scatter(d, val, color="red", s=20, zorder=5, alpha=0.6)

    ax1.set_title(f"Long-Term Backtest — MA{result.config_snapshot['ma_period']}, "
                  f"Freq={result.config_snapshot['rebalance_freq']}")
    ax1.set_ylabel("Equity (log scale)")
    ax1.set_yscale("log")
    ax1.grid(True, alpha=0.3)
    ax1.legend()

    # 回撤曲线
    dd = result.drawdown_series
    ax2.fill_between(dd.index, dd.values * 100, 0,
                      color="red", alpha=0.3, label="Drawdown")
    ax2.set_ylabel("Drawdown %")
    ax2.set_xlabel("Date")
    ax2.grid(True, alpha=0.3)
    ax2.legend()

    if save_path:
        plt.savefig(save_path, dpi=150, bbox_inches="tight")
        logger.info("权益曲线已保存: %s", save_path)
    else:
        plt.show()
    plt.close()


def plot_monthly_heatmap(result: LongTermResult, save_path: str | None = None):
    """月度收益率热力图。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import seaborn as sns

    monthly = result.monthly_returns.copy()
    # 转为 Year x Month 表格
    heatmap_data = monthly.groupby(
        [monthly.index.year, monthly.index.month]
    ).apply(lambda x: x.iloc[0]) * 100
    heatmap_df = heatmap_data.unstack(level=1)
    heatmap_df.columns = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    fig, ax = plt.subplots(figsize=(14, max(4, len(heatmap_df) * 0.4)))
    sns.heatmap(heatmap_df, annot=True, fmt=".1f", cmap="RdYlGn",
                center=0, ax=ax, cbar_kws={"label": "Monthly Return %"})
    ax.set_title(f"Monthly Returns — Strategy (MA{result.config_snapshot['ma_period']})")
    ax.set_ylabel("Year")

    if save_path:
        plt.savefig(save_path, dpi=150, bbox_inches="tight")
        logger.info("月度热力图已保存: %s", save_path)
    plt.close()


# 暴露 drawdown 属性（方便外部使用）
@property
def _drawdown_series(self: LongTermResult) -> pd.Series:
    if len(self.equity_curve) < 2:
        return pd.Series(dtype=float)
    cummax = self.equity_curve.cummax()
    return (self.equity_curve - cummax) / cummax


LongTermResult.drawdown_series = _drawdown_series
