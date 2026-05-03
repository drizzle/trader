"""Simple SMA crossover strategy on BTC/USD.

Why a separate file (vs reusing SmaCrossoverStrategy)?
  - We want is_crypto=True so the runtime knows to use the crypto data client,
    skip equity market-hours, and submit fractional GTC orders.
  - Sensible defaults differ: BTC is more volatile and trades 24/7, so faster
    SMAs (20/50) tend to be more useful than the 50/200 typical for SPY.

Logic (textbook SMA crossover, no look-ahead):
    Use bars up to t-1 (yesterday's close) to decide today's allocation.
    fast SMA  > slow SMA  → long target_allocation
    fast SMA <= slow SMA  → flat (0%)

⚠️  This is a reference strategy. Run it on Alpaca paper trading for weeks
    before considering real capital. BTC drawdowns of 50%+ are routine.
"""
from __future__ import annotations

import pandas as pd
from loguru import logger

from .base import Signal, Strategy


class BtcSmaStrategy(Strategy):
    name = "btc_sma"
    is_crypto = True

    def __init__(
        self,
        target_symbol: str = "BTC/USD",
        fast_window: int = 20,
        slow_window: int = 50,
        target_allocation: float = 0.95,
    ):
        if fast_window >= slow_window:
            raise ValueError("fast_window must be < slow_window")
        if not (0.0 <= target_allocation <= 1.0):
            raise ValueError("target_allocation must be in [0, 1]")
        self.target_symbol = target_symbol
        self.fast_window = fast_window
        self.slow_window = slow_window
        self.target_allocation = target_allocation

    @property
    def universe(self) -> list[str]:
        return [self.target_symbol]

    def compute(self, bars: dict[str, pd.DataFrame]) -> list[Signal]:
        df = bars.get(self.target_symbol)
        if df is None or len(df) < self.slow_window + 1:
            logger.debug(
                f"Not enough bars for {self.target_symbol} "
                f"(have {0 if df is None else len(df)}, need {self.slow_window + 1})"
            )
            return [Signal(self.target_symbol, 0.0, "insufficient data — flat")]

        # Decide for today using bars through yesterday — no look-ahead.
        closes = df["close"].iloc[:-1]
        fast = closes.rolling(self.fast_window).mean().iloc[-1]
        slow = closes.rolling(self.slow_window).mean().iloc[-1]

        if pd.isna(fast) or pd.isna(slow):
            return [Signal(self.target_symbol, 0.0, "SMAs not yet available")]

        if fast > slow:
            return [
                Signal(
                    self.target_symbol,
                    self.target_allocation,
                    f"long: SMA{self.fast_window}={fast:,.2f} > "
                    f"SMA{self.slow_window}={slow:,.2f}",
                )
            ]
        return [
            Signal(
                self.target_symbol,
                0.0,
                f"flat: SMA{self.fast_window}={fast:,.2f} <= "
                f"SMA{self.slow_window}={slow:,.2f}",
            )
        ]
