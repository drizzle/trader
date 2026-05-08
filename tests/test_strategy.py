"""Smoke tests for the SMA crossover strategy and risk module.

These run without any Alpaca credentials — strategies + risk are pure logic.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from alpaca.trading.enums import OrderSide

from trader.config import RiskConfig
from trader.backtest import run_backtest
from trader.main import _enforce_cash_buffer, _scale_signals_for_risk_limits
from trader.risk import RiskCheck
from trader.composer_research import parse_composer_json
from trader.storage import Storage
from trader.strategy.base import Signal, Strategy
from trader.strategy.sma_crossover import SmaCrossoverStrategy
from trader.strategy import yypt_tqqq_rsi
from trader.strategy.composer_strategy import ComposerStrategy
from trader.strategy.yypt_tqqq_rsi import YyptTqqqRsiStrategy


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


def _yypt_targets(signals):
    return {s.symbol: s.target_pct for s in signals}


def _patch_yypt_indicators(monkeypatch, rsi_value: float, sma_values: dict[int, float]):
    def fake_rsi(series, period):
        return pd.Series([rsi_value] * len(series), index=series.index)

    def fake_sma(series, window):
        return pd.Series([sma_values[window]] * len(series), index=series.index)

    monkeypatch.setattr(yypt_tqqq_rsi, "rsi", fake_rsi)
    monkeypatch.setattr(yypt_tqqq_rsi, "sma", fake_sma)


@pytest.mark.parametrize(
    "rsi_value,sma_values,expected_symbol",
    [
        (20.0, {200: 999.0, 20: 999.0}, "TQQQ"),
        (85.0, {200: 0.0, 20: 0.0}, "SHV"),
        (50.0, {200: 90.0, 20: 999.0}, "TQQQ"),
        (50.0, {200: 110.0, 20: 105.0}, "SHV"),
        (50.0, {200: 110.0, 20: 95.0}, "TQQQ"),
    ],
)
def test_yypt_strategy_matches_composer_json_tree(
    monkeypatch, rsi_value, sma_values, expected_symbol
):
    _patch_yypt_indicators(monkeypatch, rsi_value, sma_values)
    strat = YyptTqqqRsiStrategy(target_allocation=1.0)
    bars = {
        "TQQQ": _bars_from_closes([100.0] * 220),
        "SHV": _bars_from_closes([100.0] * 220),
    }

    targets = _yypt_targets(strat.compute(bars))

    assert targets[expected_symbol] == pytest.approx(1.0)
    other_symbol = "SHV" if expected_symbol == "TQQQ" else "TQQQ"
    assert targets[other_symbol] == pytest.approx(0.0)


def test_yypt_strategy_uses_latest_supplied_bar_as_current_price(monkeypatch):
    _patch_yypt_indicators(monkeypatch, 50.0, {200: 100.0, 20: 100.0})
    strat = YyptTqqqRsiStrategy(target_allocation=1.0)
    bars = {
        "TQQQ": _bars_from_closes([80.0] * 219 + [120.0]),
        "SHV": _bars_from_closes([100.0] * 220),
    }

    targets = _yypt_targets(strat.compute(bars))

    assert targets["TQQQ"] == pytest.approx(1.0)
    assert targets["SHV"] == pytest.approx(0.0)


class _WarmupStrategy(Strategy):
    name = "warmup"

    @property
    def universe(self) -> list[str]:
        return ["SPY"]

    def compute(self, bars: dict[str, pd.DataFrame]) -> list[Signal]:
        target = 1.0 if len(bars["SPY"]) >= 5 else 0.0
        return [Signal("SPY", target, f"history={len(bars['SPY'])}")]


class _CashBufferStrategy(Strategy):
    name = "cash-buffer-test"

    @property
    def universe(self) -> list[str]:
        return ["BTC/USD"]

    def compute(self, bars: dict[str, pd.DataFrame]) -> list[Signal]:
        return []


class _Alerts:
    def __init__(self):
        self.messages: list[str] = []

    def send(self, message: str) -> None:
        self.messages.append(message)


class _Execution:
    def __init__(self, pending=None):
        self.pending = pending or {}
        self.submitted: list[tuple[str, float, OrderSide, str]] = []

    def open_orders_for(self, symbol: str) -> list[object]:
        return self.pending.get(symbol, [])

    def submit_market_order(
        self, *, symbol: str, qty: float, side: OrderSide, strategy: str
    ) -> str:
        self.submitted.append((symbol, qty, side, strategy))
        return f"order-{len(self.submitted)}"


def test_backtest_uses_pre_start_history_for_indicator_warmup():
    bars = {"SPY": _bars_from_closes([100.0] * 10)}

    result = run_backtest(
        strategy=_WarmupStrategy(),
        bars=bars,
        initial_cash=10_000,
        start="2024-01-08",
    )

    assert result.start == bars["SPY"].index[5]
    assert result.trades[0].timestamp == bars["SPY"].index[5]
    assert result.trades[0].rationale == "history=5"


def test_cash_buffer_waits_for_pending_broker_order():
    with tempfile.TemporaryDirectory() as tmp:
        storage = Storage(Path(tmp) / "test.db")
        execution = _Execution(pending={"SPY": [SimpleNamespace(client_order_id="pending")]})

        handled = _enforce_cash_buffer(
            cfg=SimpleNamespace(risk=RiskConfig(min_cash_buffer_pct=0.2)),
            account=SimpleNamespace(equity=1000.0, cash=0.0),
            positions={
                "SPY": SimpleNamespace(symbol="SPY", qty=10.0, market_value=1000.0),
            },
            execution=execution,
            storage=storage,
            strategy=_CashBufferStrategy(),
            alerts=_Alerts(),
        )

        assert handled is True
        assert execution.submitted == []


def test_cash_buffer_waits_for_pending_local_order():
    with tempfile.TemporaryDirectory() as tmp:
        storage = Storage(Path(tmp) / "test.db")
        storage.record_order_submitted(
            client_order_id="trader-local-pending",
            broker_order_id=None,
            symbol="SPY",
            side="sell",
            qty=1.0,
            order_type="market",
            limit_price=None,
            strategy="cash-buffer-test:cash_buffer",
        )
        execution = _Execution()

        handled = _enforce_cash_buffer(
            cfg=SimpleNamespace(risk=RiskConfig(min_cash_buffer_pct=0.2)),
            account=SimpleNamespace(equity=1000.0, cash=0.0),
            positions={
                "SPY": SimpleNamespace(symbol="SPY", qty=10.0, market_value=1000.0),
            },
            execution=execution,
            storage=storage,
            strategy=_CashBufferStrategy(),
            alerts=_Alerts(),
        )

        assert handled is True
        assert execution.submitted == []


def test_cash_buffer_submits_once_without_pending_orders():
    with tempfile.TemporaryDirectory() as tmp:
        storage = Storage(Path(tmp) / "test.db")
        execution = _Execution()

        handled = _enforce_cash_buffer(
            cfg=SimpleNamespace(risk=RiskConfig(min_cash_buffer_pct=0.2)),
            account=SimpleNamespace(equity=1000.0, cash=0.0),
            positions={
                "SPY": SimpleNamespace(symbol="SPY", qty=10.0, market_value=1000.0),
            },
            execution=execution,
            storage=storage,
            strategy=_CashBufferStrategy(),
            alerts=_Alerts(),
        )

        assert handled is True
        assert execution.submitted == [
            ("SPY", 2.0, OrderSide.SELL, "cash-buffer-test:cash_buffer")
        ]


def test_live_signals_scale_to_aggregate_exposure_cap_for_multi_leg_strategy():
    cfg = SimpleNamespace(risk=RiskConfig(max_position_pct=0.8, min_cash_buffer_pct=0.1))
    signals = [
        Signal("AAA", 0.60, "composer:test"),
        Signal("BBB", 0.35, "composer:test"),
        Signal("CCC", 0.00, "flat (composer)"),
    ]

    scaled = _scale_signals_for_risk_limits(cfg, signals)

    assert sum(s.target_pct for s in scaled) == pytest.approx(0.80)
    assert scaled[0].target_pct == pytest.approx(0.60 * (0.80 / 0.95))
    assert scaled[1].target_pct == pytest.approx(0.35 * (0.80 / 0.95))
    assert scaled[2].target_pct == 0.0
    assert "exposure cap" in scaled[0].rationale


def test_composer_cash_fallback_allocates_residual_to_sgov():
    spec = {
        "name": "fallback-test",
        "step": "root",
        "children": [{
            "step": "wt-cash-specified",
            "children": [{
                "step": "asset",
                "ticker": "AAA",
                "weight": {"num": "50", "den": "100"},
            }],
        }],
    }
    strat = ComposerStrategy(
        spec=spec,
        name="fallback-test",
        target_allocation=0.90,
        cash_fallback_symbol="SGOV",
    )

    targets = {s.symbol: s.target_pct for s in strat.compute({"AAA": _bars_from_closes([1.0])})}
    rationales = {s.symbol: s.rationale for s in strat.compute({"AAA": _bars_from_closes([1.0])})}

    assert strat.universe == ["AAA", "SGOV"]
    assert targets["AAA"] == pytest.approx(0.45)
    assert targets["SGOV"] == pytest.approx(0.45)
    assert rationales["SGOV"] == "cash fallback:fallback-test"


def test_risk_position_size_limit():
    with tempfile.TemporaryDirectory() as tmp:
        storage = Storage(Path(tmp) / "test.db")
        risk = RiskCheck(RiskConfig(max_position_pct=0.5), storage)
        assert risk.check_position_size("SPY", 0.6, 100_000).allow is True
        assert risk.check_position_size("SPY", -0.1, 100_000).allow is False


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


def test_storage_records_latest_position_snapshot():
    with tempfile.TemporaryDirectory() as tmp:
        storage = Storage(Path(tmp) / "test.db")
        ts = storage.record_position_snapshot([
            {
                "symbol": "SPY",
                "qty": 13,
                "market_value": 9349.60,
                "avg_entry_price": 717.46,
            }
        ])
        latest_ts, positions = storage.latest_position_snapshot()
        assert latest_ts == ts
        assert positions == [{
            "symbol": "SPY",
            "qty": 13.0,
            "market_value": 9349.60,
            "avg_entry_price": 717.46,
        }]

        empty_ts = storage.record_position_snapshot([])
        latest_ts, positions = storage.latest_position_snapshot()
        assert latest_ts == empty_ts
        assert positions == []


def test_staging_strategy_switch_replaces_existing_pending_action():
    with tempfile.TemporaryDirectory() as tmp:
        storage = Storage(Path(tmp) / "test.db")
        first = storage.stage_strategy_switch(
            "btc_sma", flatten=True, restart=True, reason="first"
        )
        second = storage.stage_strategy_switch(
            "Golden Tech", flatten=True, restart=True, reason="second"
        )
        pending = storage.latest_pending_action("strategy_switch")
        assert pending["id"] == second
        assert pending["id"] != first
        assert pending["payload"]["name"] == "Golden Tech"


def test_parse_composer_json_assigns_name():
    spec = parse_composer_json('{"step":"root","children":[]}', "test_symphony")
    assert spec["name"] == "test_symphony"
    assert spec["step"] == "root"
