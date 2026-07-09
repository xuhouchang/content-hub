"""
建议日志记录。

每次 run_daily.py 生成建议后，将完整建议保存为 JSONL。
日志文件位于 data_cache/signal_log.jsonl。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import numpy as np

import pandas as pd

from config.settings import get_settings

logger = logging.getLogger(__name__)

HKT = timezone(timedelta(hours=8))
LOG_PATH = Path("data_cache/signal_log.jsonl")


def _json_serialize(obj: Any) -> str:
    """序列化 JSON，处理 numpy 类型。"""
    class _Encoder(json.JSONEncoder):
        def default(self, o: Any) -> Any:
            if isinstance(o, (np.integer, np.int64)):
                return int(o)
            if isinstance(o, (np.floating, np.float64)):
                return float(o)
            if isinstance(o, np.bool_):
                return bool(o)
            return super().default(o)
    return json.dumps(obj, ensure_ascii=False, cls=_Encoder)


def _score_snapshot(
    rotation_result: Any,
) -> dict[str, dict[str, float]]:
    """从 rotation_result.all_ranked 提取各标的信号快照。"""
    if rotation_result.all_ranked is None or rotation_result.all_ranked.empty:
        return {}

    ranked = rotation_result.all_ranked
    snapshot: dict[str, dict[str, float]] = {}

    for ticker, row in ranked.iterrows():
        snapshot[ticker] = {
            "ret_20": float(row.get("ret_20", 0)),
            "ret_60": float(row.get("ret_60", 0)),
            "hv_20d": float(row.get("hv_20d", 0)),
            "momentum_score": float(row.get("momentum_score", 0)),
            "composite_score": float(row.get("composite_score", 0)),
            "rank": int(row.get("rank", 0)),
        }

    return snapshot


def _trend_snapshot(
    close_prices: dict[str, pd.Series] | None,
) -> dict[str, str]:
    """获取各标的趋势状态。"""
    if not close_prices:
        return {}

    from signals.trend import trend_state

    result: dict[str, str] = {}
    for ticker, series in close_prices.items():
        if series.empty:
            continue
        try:
            result[ticker] = trend_state(series)
        except Exception:
            result[ticker] = "未知"
    return result


def _risk_level(rotation_result: Any) -> str:
    """根据轮动结果判断风险等级。"""
    if rotation_result.is_defensive:
        return "high"
    if rotation_result.all_ranked is not None and not rotation_result.all_ranked.empty:
        hv_col = "hv_20d"
        top = rotation_result.all_ranked.head(1)
        hv = top.get(hv_col, pd.Series([0])).iloc[0]
        if hv > 0.3:
            return "medium"
    return "low"


def _load_dates() -> set[str]:
    """读取已有的 signal_date 列表，用于去重。"""
    log_path = LOG_PATH
    if not log_path.exists():
        return set()
    dates: set[str] = set()
    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                dates.add(rec.get("signal_date", ""))
            except json.JSONDecodeError:
                continue
    return dates


def save_signal(
    rotation_result: Any,
    position_result: Any,
    close_prices: dict[str, pd.Series] | None = None,
    signal_date: str | None = None,
) -> None:
    """将当前建议保存为一条 JSONL 记录。

    Args:
        rotation_result: RotationSignal 对象
        position_result: PositionAllocation 对象
        close_prices: 用于提取趋势状态
        signal_date: 日期 YYYY-MM-DD，默认今天
    """
    settings = get_settings()

    if signal_date is None:
        signal_date = datetime.now(HKT).strftime(settings.report.date_format)

    # ── 去重：如果该日期已有信号，覆盖旧记录 ──
    existing_dates = _load_dates()
    if signal_date in existing_dates:
        logger.info("信号日期 %s 已存在，覆盖更新", signal_date)
        # 读取全部，跳过该日期的旧记录，末尾追加新记录
        log_path = LOG_PATH
        kept: list[str] = []
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get("signal_date", "") == signal_date:
                        continue  # 跳过这条旧记录
                except json.JSONDecodeError:
                    pass
                kept.append(line)

        # 写回（截断文件，保留其他日期记录）
        with open(log_path, "w", encoding="utf-8") as f:
            for line in kept:
                f.write(line + "\n")

    generated_at = datetime.now(HKT).isoformat(timespec="seconds")

    # 从 recommended_weights 提取选中 ETF
    selected_etf = [
        t for t, w in position_result.allocations.items()
        if w > 0
    ]

    record = {
        "signal_date": signal_date,
        "recommended_weights": {
            **position_result.allocations,
            "CASH": position_result.cash_weight,
        },
        "selected_etf": selected_etf,
        "benchmark": settings.universe.tickers[0],  # 02800.HK
        "score_snapshot": _score_snapshot(rotation_result),
        "trend_snapshot": _trend_snapshot(close_prices),
        "risk_level": _risk_level(rotation_result),
        "reasons": [rotation_result.reason] if rotation_result.reason else [],
        "is_defensive": rotation_result.is_defensive,
        "generated_at": generated_at,
    }

    # 写入 JSONL（处理 numpy 类型）
    log_path = LOG_PATH
    log_path.parent.mkdir(parents=True, exist_ok=True)

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(_json_serialize(record) + "\n")

    logger.info("信号日志已保存: %s", signal_date)


def load_signals(
    limit: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict]:
    """读取历史信号日志。

    Args:
        limit: 限制返回条数（最近 N 条）
        start_date: 起始日期 YYYY-MM-DD
        end_date: 结束日期 YYYY-MM-DD

    Returns:
        list[dict]，按日期降序排列
    """
    log_path = LOG_PATH
    if not log_path.exists():
        return []

    records: list[dict] = []
    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("跳过无效日志行: %s", line[:50])

    # 过滤日期范围
    if start_date:
        records = [r for r in records if r.get("signal_date", "") >= start_date]
    if end_date:
        records = [r for r in records if r.get("signal_date", "") <= end_date]

    # 按日期降序
    records.sort(key=lambda r: r.get("signal_date", ""), reverse=True)

    if limit:
        records = records[:limit]

    return records
