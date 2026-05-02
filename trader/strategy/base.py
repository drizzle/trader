"""Strategy abstract base class.

A strategy takes the latest market data + current portfolio state and returns
a list of TARGET allocations (not orders). The execution layer turns targets
into orders via diff against current positions.

This separation matters: it makes the strategy fully unit-testable and ensures
the same code runs in backtest and live (no order-state assumptions baked in).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import pandas as pd


@dataclass
class Signal:
    """Target allocation for a symbol, expressed as a fraction of total equity."""
    symbol: str
    target_pct: float       # 0.0 = flat, 1.0 = 100% of equity in this name
    rationale: str = ""     # human-readable why; logged + stored


class Strategy(ABC):
    """Subclass me. Override .compute(). Keep state out of __init__ (use params)."""

    name: str = "unnamed"

    @property
    @abstractmethod
    def universe(self) -> list[str]:
        """Tickers this strategy needs daily bars for."""
        ...

    @abstractmethod
    def compute(self, bars: dict[str, pd.DataFrame]) -> list[Signal]:
        """Given latest bars per symbol, return target allocations.

        Important: do NOT use the most recent bar's close to make decisions for
        that same bar (look-ahead bias). Use bars up to t-1 to decide for t.
        """
        ...
