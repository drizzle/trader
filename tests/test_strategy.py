"""Smoke tests for the SMA crossover strategy and risk module.

These run without any Alpaca credentials — strategies + risk are pure logic.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from trader.config import RiskConfig
from trader.risk import RiskCheck
from trader.storage import Storage
from trader.strategy.sma_crossover import SmaCrossoverStrategy


def _bars_from_closes(closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=len(closes), freq="B")
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [1_000_000] * len(closes),
        },
        index=idx,
    )


def test_sma_strategy_long_when_uptrend():
    # Strong uptrend: fast SMA will be above slow SMA.
    closes = list(np.linspace(100, 200, 250))
    strat = SmaCrossoverStrategy(target_symbol="SPY", fast_window=10, slow_window=50,
                                 target_allocation=0.95)
    signals = strat.compute({"SPY": _bars_from_closes(closes)})
    assert len(signals) == 1
    assert signals[0].symbol == "SPY"
    assert signals[0].target_pct == pytest.approx(0.95)


def test_sma_strategy_flat_when_downtrend():
    closes = list(np.linspace(200, 100, 250))
    strat = SmaCrossoverStrategy(target_symbol="SPY", fast_window=10, slow_window=50,
                                 target_allocation=0.95)
    signals = strat.compute({"SPY": _bars_from_closes(closes)})
    assert signals[0].target_pct == 0.0


def test_sma_strategy_insufficient_data():
    closes = [100.0] * 20
    strat = SmaCrossoverStrategy(target_symbol="SPY", fast_window=10, slow_window=50)
    signals = strat.compute({"SPY": _bars_from_closes(closes)})
    assert signals[0].target_pct == 0.0
    assert "insufficient" in signals[0].rationale


def test_sma_strategy_validates_window_order():
    with pytest.raises(ValueError):
        SmaCrossoverStrategy(fast_window=200, slow_window=50)


def test_risk_position_size_limit():
    with tempfile.TemporaryDirectory() as tmp:
        storage = Storage(Path(tmp) / "test.db")
        risk = RiskCheck(RiskConfig(max_position_pct=0.5), storage)
        assert risk.check_position_size("SPY", 0.4, 100_000).allow is True
        assert risk.check_position_size("SPY", 0.6, 100_000).allow is False


def test_risk_cash_buffer():
    with tempfile.TemporaryDirectory() as tmp:
        storage = Storage(Path(tmp) / "test.db")
        risk = RiskCheck(RiskConfig(min_cash_buffer_pct=0.05), storage)
        assert risk.check_cash_buffer(0.94).allow is True
        assert risk.check_cash_buffer(0.96).allow is False


def test_kill_switch():
    with tempfile.TemporaryDirectory() as tmp:
        storage = Storage(Path(tmp) / "test.db")
        kill_path = Path(tmp) / "STOP"
        risk = RiskCheck(RiskConfig(kill_switch_path=str(kill_path)), storage)
        assert risk.kill_switch_engaged() is False
        kill_path.write_text("halt")
        assert risk.kill_switch_engaged() is True


def test_storage_records_and_reads_equity():
    with tempfile.TemporaryDirectory() as tmp:
        storage = Storage(Path(tmp) / "test.db")
        storage.record_equity(cash=10000, equity=100000, buying_power=200000)
        # ts in the future returns the most recent snapshot
        eq = storage.equity_at_or_before("2099-01-01T00:00:00+00:00")
        assert eq == 100000.0
