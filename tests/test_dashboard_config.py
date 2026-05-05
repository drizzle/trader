from __future__ import annotations

from trader.cli import _apply_strategy_update
from trader.dashboard.app import _apply_risk_update, _restart_trader_service


def _config() -> dict:
    return {
        "universe": ["BTC/USD"],
        "strategy": {
            "name": "btc_sma",
            "params": {
                "target_symbol": "BTC/USD",
                "target_allocation": 0.95,
            },
        },
        "schedule": {"interval_minutes": 15},
        "risk": {
            "max_position_pct": 0.7,
            "daily_loss_limit_pct": 0.1,
            "min_cash_buffer_pct": 0.2,
            "kill_switch_path": "./data/STOP",
        },
        "storage": {"db_filename": "trader.db"},
    }


def test_risk_update_preserves_deployed_strategy() -> None:
    raw = _config()

    updated = _apply_risk_update(
        raw,
        {
            "max_position_pct": 0.5,
            "daily_loss_limit_pct": 0.04,
            "min_cash_buffer_pct": 0.1,
        },
        "./data/STOP",
    )

    assert updated["strategy"] == raw["strategy"]
    assert updated["universe"] == raw["universe"]
    assert updated["risk"] == {
        "max_position_pct": 0.5,
        "daily_loss_limit_pct": 0.04,
        "min_cash_buffer_pct": 0.1,
        "kill_switch_path": "./data/STOP",
    }


def test_strategy_update_preserves_deployed_risk() -> None:
    raw = _config()

    updated = _apply_strategy_update(
        raw,
        "sma_crossover",
        {
            "target_symbol": "SPY",
            "target_allocation": 0.95,
            "fast_window": 50,
            "slow_window": 200,
        },
        ["SPY"],
    )

    assert updated["risk"] == raw["risk"]
    assert updated["schedule"] == raw["schedule"]
    assert updated["strategy"]["name"] == "sma_crossover"
    assert updated["universe"] == ["SPY"]


def test_restart_trader_uses_sudoers_systemctl_path(monkeypatch) -> None:
    calls = []

    class Result:
        stdout = "active\n"
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return Result()

    monkeypatch.delenv("SYSTEMCTL_BIN", raising=False)
    monkeypatch.setattr("trader.dashboard.app.subprocess.run", fake_run)

    ok, detail = _restart_trader_service()

    assert ok is True
    assert detail == "trader restarted"
    assert calls == [
        ["sudo", "-n", "/bin/systemctl", "restart", "trader"],
        ["sudo", "-n", "/bin/systemctl", "is-active", "trader"],
    ]
