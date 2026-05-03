"""Build a MarketContext from raw Alpaca daily bars.

Pure functions (no I/O) — the caller fetches bars via DataClient and passes
them in. That keeps this module unit-testable without API access.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import pandas as pd

from ..indicators import rsi, sma
from .base import MarketContext


def _pct_return(closes: pd.Series, lookback: int) -> float | None:
    if len(closes) <= lookback:
        return None
    end = float(closes.iloc[-1])
    start = float(closes.iloc[-1 - lookback])
    if start <= 0:
        return None
    return (end / start - 1.0) * 100.0


def _annualized_vol(closes: pd.Series, window: int) -> float | None:
    if len(closes) < window + 1:
        return None
    log_returns = (closes / closes.shift(1)).apply(lambda x: math.log(x) if x and x > 0 else 0.0)
    recent = log_returns.iloc[-window:]
    if recent.std() == 0:
        return 0.0
    return float(recent.std() * math.sqrt(252) * 100.0)


def build_context(symbol: str, bars: pd.DataFrame) -> MarketContext:
    """Compute all metrics for one symbol. `bars` must have a 'close' column."""
    closes = bars["close"].astype(float)
    last_close = float(closes.iloc[-1])

    fifty_two_window = closes.iloc[-252:] if len(closes) >= 252 else closes
    high = float(fifty_two_window.max()) if len(fifty_two_window) else None
    low = float(fifty_two_window.min()) if len(fifty_two_window) else None
    drawdown = ((last_close / high - 1.0) * 100.0) if high and high > 0 else None

    rsi_val = rsi(closes, 14).iloc[-1] if len(closes) >= 15 else None
    sma_50 = sma(closes, 50).iloc[-1] if len(closes) >= 50 else None
    sma_200 = sma(closes, 200).iloc[-1] if len(closes) >= 200 else None

    avg_vol = None
    if "volume" in bars.columns and len(bars) >= 20:
        vol_recent = bars["volume"].iloc[-20:].astype(float)
        avg_vol = float(vol_recent.mean())

    def _f(x):
        if x is None:
            return None
        if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
            return None
        return float(x)

    return MarketContext(
        symbol=symbol,
        as_of=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        last_close=last_close,
        return_1d_pct=_pct_return(closes, 1),
        return_1w_pct=_pct_return(closes, 5),
        return_1m_pct=_pct_return(closes, 20),
        return_3m_pct=_pct_return(closes, 60),
        return_1y_pct=_pct_return(closes, 252),
        fifty_two_week_high=_f(high),
        fifty_two_week_low=_f(low),
        drawdown_from_high_pct=_f(drawdown),
        realized_vol_20d_pct=_annualized_vol(closes, 20),
        realized_vol_60d_pct=_annualized_vol(closes, 60),
        rsi_14=_f(rsi_val),
        sma_50=_f(sma_50),
        sma_200=_f(sma_200),
        avg_volume_20d=_f(avg_vol),
    )
