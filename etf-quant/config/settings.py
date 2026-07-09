"""
配置加载模块。

职责：
- 从 config.yaml 加载配置
- 提供类型安全的配置对象
- 所有模块通过 settings 对象访问配置，不入参传递深层嵌套 dict
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


# ── 使用 pydantic 做运行时类型校验 ──────────────────────
try:
    from pydantic import BaseModel, Field

    class MomentumConfig(BaseModel):
        periods: list[int] = [20, 60]
        weights: list[float] = [0.4, 0.6]

    class VolatilityConfig(BaseModel):
        window: int = 20
        penalty: float = 0.2
        annualization_factor: int = 252

    class TrendConfig(BaseModel):
        ma_periods: list[int] = [20, 60]

    class PositionConfig(BaseModel):
        max_single: float = 0.5
        cash_default: float = 0.5
        defensive_on_break_ma: int = 60
        top_n: int = 1

    class BacktestConfig(BaseModel):
        years: int = 1
        rebalance_freq: str = "W"
        initial_capital: float = 1_000_000

    class DataConfig(BaseModel):
        primary_source: str = "yahoo"
        cache_dir: str = "data_cache/hist"
        max_retries: int = 3
        retry_delay_sec: int = 2

    class LoggingConfig(BaseModel):
        level: str = "INFO"
        file: str = "logs/etf-quant.log"

    class ReportConfig(BaseModel):
        output_dir: str = "reports/output"
        date_format: str = "%Y-%m-%d"

    class UniverseConfig(BaseModel):
        tickers: list[str] = ["02800.HK", "03033.HK", "02828.HK", "02840.HK"]
        defensive_tickers: list[str] = ["CASH", "02840.HK"]

    class Settings(BaseModel):
        universe: UniverseConfig = Field(default_factory=UniverseConfig)
        momentum: MomentumConfig = Field(default_factory=MomentumConfig)
        volatility: VolatilityConfig = Field(default_factory=VolatilityConfig)
        trend: TrendConfig = Field(default_factory=TrendConfig)
        position: PositionConfig = Field(default_factory=PositionConfig)
        backtest: BacktestConfig = Field(default_factory=BacktestConfig)
        data: DataConfig = Field(default_factory=DataConfig)
        logging: LoggingConfig = Field(default_factory=LoggingConfig)
        report: ReportConfig = Field(default_factory=ReportConfig)

except ImportError:
    # pydantic 不可用时退化为 dict wrapper（保持接口兼容）
    class Settings:  # type: ignore[no-redef]
        def __init__(self, raw: dict[str, Any]) -> None:
            self._raw = raw
            for section, values in raw.items():
                if isinstance(values, dict):
                    setattr(self, section, _DictObj(values))

        def dict(self) -> dict[str, Any]:
            return self._raw

    class _DictObj:
        def __init__(self, d: dict[str, Any]) -> None:
            self.__dict__["_data"] = d

        def __getattr__(self, key: str) -> Any:
            val = self._data.get(key, _MISSING)
            if val is _MISSING:
                raise AttributeError(key)
            if isinstance(val, dict):
                return _DictObj(val)
            return val

    class _MISSING:
        pass


def load_settings(path: str | Path | None = None) -> Settings:
    """加载配置。

    优先级：
    1. 环境变量 ETF_CONFIG_PATH
    2. 函数参数 path
    3. 项目默认路径 config.yaml
    """
    config_path = Path(
        os.environ.get("ETF_CONFIG_PATH")
        or path
        or DEFAULT_CONFIG_PATH
    )

    if not config_path.exists():
        raise FileNotFoundError(
            f"配置文件不存在: {config_path}"
        )

    with open(config_path, "r") as f:
        raw: dict[str, Any] = yaml.safe_load(f)

    try:
        return Settings(**raw)
    except TypeError:
        # pydantic 不可用或结构不匹配时 fallback
        return Settings(raw)


# 模块级单例（懒加载）
_settings: Settings | None = None


def get_settings() -> Settings:
    """获取全局配置单例。"""
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings
