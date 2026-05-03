"""Technical-indicator helpers. Pure pandas, no I/O.

Kept tiny on purpose — strategies should depend on this rather than
re-implementing RSI/SMA inline. That way every strategy uses the same
canonical formula and a fix here propagates everywhere.
"""
from __future__ import annotations

import pandas as pd


def sma(series: pd.Series, window: int) -> pd.Series:
    """Simple moving average. Standard pandas rolling mean."""
    return series.rolling(window=window, min_periods=window).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index using Wilder's smoothing.

    Returns values in [0, 100]. Standard interpretation:
    - RSI > 70 (or 80): overbought
    - RSI < 30 (or 20): oversold

    Wilder's smoothing is the canonical RSI formula. The exponentially
    weighted version (alpha = 1/period) matches what TradingView, Composer,
    and most charting platforms use.
    """
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)

    # Wilder's smoothing == EWM with alpha = 1/period (com = period - 1).
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    rs = avg_gain / avg_loss
    rsi_val = 100.0 - (100.0 / (1.0 + rs))
    # When avg_loss is 0, rs = inf, rsi = 100. When avg_gain is 0, rsi = 0. Pandas handles both.
    return rsi_val
