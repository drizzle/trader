"""Wraps Alpaca's market data API. Fetches historical bars for the strategy."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
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
        # Crypto endpoints are free and don't have the 15-minute IEX delay.
        self._crypto_client = CryptoHistoricalDataClient(config.api_key, config.secret_key)

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

    def bars_for_universe(
        self, symbols: list[str], lookback_days: int = 365
    ) -> dict[str, pd.DataFrame]:
        """Fetch daily bars for a mixed universe of stocks + crypto.

        Routing rule: a "/" in the symbol means crypto (e.g. BTC/USD, ETH/USD)
        and goes through the crypto endpoint; everything else hits the equity
        endpoint. Used by the dashboard's advisor + summary tabs which can
        receive any mix of tickers from the configured universe or a user
        text input.
        """
        crypto_syms = [s for s in symbols if "/" in s]
        stock_syms = [s for s in symbols if "/" not in s]

        out: dict[str, pd.DataFrame] = {}
        if stock_syms:
            try:
                out.update(self.daily_bars(stock_syms, lookback_days))
            except Exception as e:
                logger.warning(f"daily_bars failed for {stock_syms}: {e}")
                for s in stock_syms:
                    out.setdefault(s, pd.DataFrame())
        if crypto_syms:
            try:
                out.update(self.crypto_daily_bars(crypto_syms, lookback_days))
            except Exception as e:
                logger.warning(f"crypto_daily_bars failed for {crypto_syms}: {e}")
                for s in crypto_syms:
                    out.setdefault(s, pd.DataFrame())
        return out

    def crypto_daily_bars(
        self, symbols: list[str], lookback_days: int = 365
    ) -> dict[str, pd.DataFrame]:
        """Fetch daily OHLCV crypto bars (e.g. 'BTC/USD') over the lookback window.

        Crypto trades 24/7, so no calendar padding is needed — we just walk
        back `lookback_days` from now. The crypto endpoints aren't on the
        15-minute IEX delay, so `end` is `now`.
        """
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=lookback_days)

        req = CryptoBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
        )

        logger.debug(
            f"Fetching daily crypto bars for {symbols} from {start.date()} to {end.date()}"
        )
        bars = self._crypto_client.get_crypto_bars(req)
        df = bars.df  # MultiIndex: (symbol, timestamp)

        if df.empty:
            logger.warning(f"No crypto bars returned for {symbols}")
            return {sym: pd.DataFrame() for sym in symbols}

        out: dict[str, pd.DataFrame] = {}
        for sym in symbols:
            if sym in df.index.get_level_values(0):
                out[sym] = df.xs(sym, level=0).copy()
            else:
                out[sym] = pd.DataFrame()
                logger.warning(f"No bars for {sym}")
        return out
