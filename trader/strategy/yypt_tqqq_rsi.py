"""YYPT | TQQQ / SHV — Reddit-popularized leveraged ETF rotation.

Decision tree (verified against the Composer JSON export):

    IF RSI(10) of TQQQ < 30                       →  TQQQ   (oversold mean-revert)
    ELIF RSI(10) of TQQQ > 80                     →  SHV    (overbought defensive)
    ELSE  (RSI in [30, 80]):
        IF current price of TQQQ > SMA(200)       →  TQQQ   (uptrend, hold leverage)
        ELSE (downtrend):
            IF current price of TQQQ < SMA(20)    →  SHV    (downtrend AND weak short-term)
            ELSE                                  →  TQQQ   (downtrend bounce, mean-revert)

Universe: just TQQQ and SHV. The exported tree never holds QQQ or FTLT.

⚠️  RISK NOTES — READ BEFORE USING WITH REAL MONEY:
    - TQQQ is a 3x daily-leveraged ETF. Volatility decay erodes long-term
      returns vs the 3x of QQQ. Drawdowns can exceed 80%.
    - Backtests of this strategy class show outsized historical returns
      partly because TQQQ only existed during the 2010s+ bull market;
      its behavior in a sustained bear is largely untested.
    - Reddit-popularized strategies are heavily survivorship-biased.
    - The "downtrend bounce → TQQQ" branch is genuinely aggressive: it buys
      a 3x leveraged ETF DURING a long-term downtrend, on the bet that any
      short-term strength will continue. Use tight risk caps.
"""
from __future__ import annotations

import pandas as pd
from loguru import logger

from ..indicators import rsi, sma
from .base import Signal, Strategy


class YyptTqqqRsiStrategy(Strategy):
    name = "yypt_tqqq_rsi"

    def __init__(
        self,
        risk_symbol: str = "TQQQ",         # leveraged growth instrument
        defensive_symbol: str = "SHV",     # short-term treasuries (cash-equivalent)
        rsi_period: int = 10,
        rsi_low: float = 30.0,
        rsi_high: float = 80.0,
        sma_long_window: int = 200,
        sma_short_window: int = 20,
        target_allocation: float = 0.95,
    ):
        if not (0 < rsi_low < rsi_high < 100):
            raise ValueError("rsi_low must be < rsi_high and both in (0,100)")
        if sma_short_window >= sma_long_window:
            raise ValueError("sma_short_window must be < sma_long_window")
        self.risk_symbol = risk_symbol
        self.defensive_symbol = defensive_symbol
        self.rsi_period = rsi_period
        self.rsi_low = rsi_low
        self.rsi_high = rsi_high
        self.sma_long_window = sma_long_window
        self.sma_short_window = sma_short_window
        self.target_allocation = target_allocation

    @property
    def universe(self) -> list[str]:
        return [self.risk_symbol, self.defensive_symbol]

    def _allocate(self, target: str, rationale: str) -> list[Signal]:
        """Return signals that put target_allocation in `target` and 0% in the other."""
        return [
            Signal(
                s,
                self.target_allocation if s == target else 0.0,
                rationale if s == target else f"flat (target = {target})",
            )
            for s in self.universe
        ]

    def compute(self, bars: dict[str, pd.DataFrame]) -> list[Signal]:
        # All universe symbols must have data; we only USE TQQQ history for the
        # decision but we need a price for SHV when the executor sizes orders.
        for sym in self.universe:
            df = bars.get(sym)
            if df is None or df.empty:
                logger.debug(f"{self.name}: missing bars for {sym} — going flat")
                return [Signal(s, 0.0, f"missing bars for {sym}") for s in self.universe]

        risk_bars = bars[self.risk_symbol]
        if len(risk_bars) < self.sma_long_window:
            logger.debug(
                f"{self.name}: insufficient {self.risk_symbol} history "
                f"({len(risk_bars)} < {self.sma_long_window})"
            )
            return [Signal(s, 0.0, "warmup") for s in self.universe]

        # Use the latest bar supplied by the caller as Composer's "current-price".
        # The backtester already excludes the fill day, so dropping another bar
        # here would add an unintended one-day lag.
        closes = risk_bars["close"]
        last_close = float(closes.iloc[-1])
        rsi_val = float(rsi(closes, self.rsi_period).iloc[-1])
        sma_long = float(sma(closes, self.sma_long_window).iloc[-1])
        sma_short = float(sma(closes, self.sma_short_window).iloc[-1])

        if pd.isna(rsi_val) or pd.isna(sma_long) or pd.isna(sma_short):
            return [Signal(s, 0.0, "indicator NaN") for s in self.universe]

        # --- Decision tree ---
        if rsi_val < self.rsi_low:
            return self._allocate(
                self.risk_symbol,
                f"oversold: RSI{self.rsi_period}={rsi_val:.1f} < {self.rsi_low}",
            )

        if rsi_val > self.rsi_high:
            return self._allocate(
                self.defensive_symbol,
                f"overbought: RSI{self.rsi_period}={rsi_val:.1f} > {self.rsi_high}",
            )

        # Neutral RSI band — long-term trend filter.
        if last_close > sma_long:
            return self._allocate(
                self.risk_symbol,
                f"uptrend: close={last_close:.2f} > SMA{self.sma_long_window}={sma_long:.2f}",
            )

        # Long-term downtrend — short-term mean reversion filter.
        if last_close < sma_short:
            return self._allocate(
                self.defensive_symbol,
                f"downtrend, weak: close={last_close:.2f} <= SMA{self.sma_long_window}={sma_long:.2f} "
                f"AND close < SMA{self.sma_short_window}={sma_short:.2f}",
            )
        return self._allocate(
            self.risk_symbol,
            f"downtrend bounce: close={last_close:.2f} <= SMA{self.sma_long_window}={sma_long:.2f} "
            f"BUT close >= SMA{self.sma_short_window}={sma_short:.2f}",
        )
