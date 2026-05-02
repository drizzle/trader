"""Reference strategy: 50/200-day SMA crossover on a single symbol (default SPY).

This is a textbook example to verify the platform plumbing end-to-end.
It is NOT a recommendation. Real money should not touch this without your own
backtest, walk-forward validation, and weeks of paper-trading evidence.
"""
from __future__ import annotations

import pandas as pd
from loguru import logger

from .base import Signal, Strategy


class SmaCrossoverStrategy(Strategy):
    name = "sma_crossover"

    def __init__(
        self,
        target_symbol: str = "SPY",
        fast_window: int = 50,
        slow_window: int = 200,
        target_allocation: float = 0.95,
    ):
        if fast_window >= slow_window:
            raise ValueError("fast_window must be < slow_window")
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
            logger.warning(
                f"Not enough bars for {self.target_symbol} "
                f"(have {0 if df is None else len(df)}, need {self.slow_window + 1})"
            )
            return [Signal(self.target_symbol, 0.0, "insufficient data — flat")]

        # Use bars up to t-1 (yesterday's close) to decide for today.
        # This avoids look-ahead bias and matches what would happen in backtest.
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
                    f"long: SMA{self.fast_window}={fast:.2f} > SMA{self.slow_window}={slow:.2f}",
                )
            ]
        else:
            return [
                Signal(
                    self.target_symbol,
                    0.0,
                    f"flat: SMA{self.fast_window}={fast:.2f} <= SMA{self.slow_window}={slow:.2f}",
                )
            ]
