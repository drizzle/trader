"""Wraps Alpaca's market data API. Fetches historical bars for the strategy."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from loguru import logger

from .config import AlpacaConfig


class DataClient:
    def __init__(self, config: AlpacaConfig):
        # Data API uses the same keys regardless of paper vs live.
        self._client = StockHistoricalDataClient(config.api_key, config.secret_key)

    def daily_bars(
        self, symbols: list[str], lookback_days: int = 365
    ) -> dict[str, pd.DataFrame]:
        """Fetch daily OHLCV bars for each symbol over the lookback window.

        Returns a dict mapping symbol -> DataFrame (indexed by timestamp).
        """
        end = datetime.now(timezone.utc)
        # Add slack so we have enough trading days even after weekends/holidays.
        start = end - timedelta(days=lookback_days + 30)

        req = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
        )

        logger.debug(f"Fetching daily bars for {symbols} from {start.date()} to {end.date()}")
        bars = self._client.get_stock_bars(req)
        df = bars.df  # MultiIndex: (symbol, timestamp)

        if df.empty:
            logger.warning(f"No bars returned for {symbols}")
            return {sym: pd.DataFrame() for sym in symbols}

        out: dict[str, pd.DataFrame] = {}
        for sym in symbols:
            if sym in df.index.get_level_values(0):
                out[sym] = df.xs(sym, level=0).copy()
            else:
                out[sym] = pd.DataFrame()
                logger.warning(f"No bars for {sym}")
        return out
