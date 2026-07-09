"""
Yahoo Finance 数据源实现。

使用 yfinance 库获取港股 ETF 日线数据。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd

from config.settings import get_settings
from .base import DataSource, FetchResult

logger = logging.getLogger(__name__)


class YahooSource(DataSource):
    """Yahoo Finance 数据源。"""

    @property
    def name(self) -> str:
        return "yahoo"

    def fetch(
        self,
        tickers: list[str],
        start: str,
        end: str | None = None,
    ) -> FetchResult:
        import yfinance as yf

        settings = get_settings()
        max_retries = settings.data.max_retries
        retry_delay = settings.data.retry_delay_sec

        result_data: dict[str, pd.DataFrame] = {}
        errors: dict[str, str] = {}

        for ticker in tickers:
            if ticker == "CASH":
                # CASH 不参与数据获取
                continue

            # Yahoo Finance 港股 ticker 格式: 去掉前导零 (02800.HK -> 2800.HK)
            yahoo_ticker = self._to_yahoo_ticker(ticker)

            for attempt in range(max_retries):
                try:
                    logger.info(
                        "获取数据 [%s -> %s] [%s ~ %s] (attempt %d/%d)",
                        ticker, yahoo_ticker, start, end or "now",
                        attempt + 1, max_retries,
                    )
                    df = yf.download(
                        tickers=yahoo_ticker,
                        start=start,
                        end=end,
                        progress=False,
                        auto_adjust=False,  # 保留原始数据，自己处理复权
                    )

                    if df.empty:
                        errors[ticker] = "返回空数据"
                        logger.warning("空数据 [%s]", ticker)
                        break

                    # 标准化列名 (yfinance 返回 MultiIndex 的列)
                    df = self._standardize_columns(df)
                    df = self.validate_data(df)
                    result_data[ticker] = df
                    break

                except Exception as e:
                    logger.warning(
                        "获取失败 [%s] attempt %d/%d: %s",
                        ticker, attempt + 1, max_retries, e,
                    )
                    if attempt < max_retries - 1:
                        import time
                        time.sleep(retry_delay)
                    else:
                        errors[ticker] = str(e)

        return FetchResult(
            data=result_data,
            source_name=self.name,
            fetched_at=datetime.now(timezone.utc),
            errors=errors,
        )

    @staticmethod
    def _to_yahoo_ticker(ticker: str) -> str:
        """将港股 ticker 转为 Yahoo Finance 可识别的格式。

        02800.HK -> 2800.HK
        03033.HK -> 3033.HK
        02828.HK -> 2828.HK
        02840.HK -> 2840.HK
        """
        if ticker.endswith(".HK") and ticker[0] == "0":
            # 去掉前导零
            code = ticker.replace(".HK", "").lstrip("0")
            return f"{code}.HK"
        return ticker

    @staticmethod
    def _standardize_columns(df: pd.DataFrame) -> pd.DataFrame:
        """标准化列名为大写首字母格式。

        yfinance 返回可能的列名格式:
        - ('Adj Close', '02800.HK') -> 'Adj Close'
        - 'Close' -> 'Close'
        """
        if isinstance(df.columns, pd.MultiIndex):
            # 格式: ('Close', '02800.HK')
            df.columns = [col[0] for col in df.columns]
        elif isinstance(df.columns, pd.Index):
            df.columns = [str(c) for c in df.columns]

        # 统一列名
        rename_map = {
            "Adj Close": "Adj Close",
            "adjclose": "Adj Close",
            "Adj_Close": "Adj Close",
        }
        df = df.rename(columns=rename_map)

        # 确保 Adj Close 列存在（没有的话用 Close 代替）
        if "Adj Close" not in df.columns and "Close" in df.columns:
            df["Adj Close"] = df["Close"]

        # 确保列顺序一致
        expected = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
        available = [c for c in expected if c in df.columns]
        return df[available]
