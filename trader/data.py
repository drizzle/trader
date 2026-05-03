"""Wraps Alpaca's market data API. Fetches historical bars for the strategy."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from loguru import logger

from .config import AlpacaConfig


# Free Alpaca accounts can only access the IEX feed (not SIP) and have a
# ~15-minute restriction on the most recent data. We pad the end time to
# stay safely outside that window.
_FREE_TIER_DELAY_MINUTES = 20


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
        # Stay outside the free-tier 15-minute restriction.
        end = datetime.now(timezone.utc) - timedelta(minutes=_FREE_TIER_DELAY_MINUTES)
        # Add slack so we have enough trading days even after weekends/holidays.
        start = end - timedelta(days=lookback_days + 30)

        req = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            adjustment=Adjustment.ALL,
            feed=DataFeed.IEX,   # free tier; switch to DataFeed.SIP if you upgrade
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
