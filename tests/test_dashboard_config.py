from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from trader.cli import _apply_strategy_update
from trader.config import load_config
from trader.dashboard.app import (
    _apply_risk_update,
    _read_latest_signal,
    _read_signals,
    _restart_trader_service,
    create_app,
)
from trader.storage import Storage


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


def test_dashboard_config_does_not_require_alpaca_keys(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
universe: [BTC/USD]
strategy:
  name: btc_sma
  params: {}
schedule:
  interval_minutes: 15
risk:
  max_position_pct: 1.0
  daily_loss_limit_pct: 0.03
  min_cash_buffer_pct: 0.02
  kill_switch_path: ./data/STOP
storage:
  db_filename: trader.db
"""
    )

    cfg = load_config(config_path, require_alpaca=False)

    assert cfg.alpaca.api_key == "dashboard-disabled"
    assert cfg.alpaca.secret_key == "dashboard-disabled"


def test_ira_account_rejects_crypto_strategy(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ROTH_IRA_ALPACA_API_KEY", "key")
    monkeypatch.setenv("ROTH_IRA_ALPACA_SECRET_KEY", "secret")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
active_account: Roth_IRA
accounts:
  - id: Roth_IRA
    type: ira
    env_prefix: ROTH_IRA
    live: true
universe: [BTC/USD]
strategy:
  name: btc_sma
  params:
    target_symbol: BTC/USD
schedule:
  interval_minutes: 15
risk:
  max_position_pct: 1.0
  daily_loss_limit_pct: 0.03
  min_cash_buffer_pct: 0.02
  kill_switch_path: ./data/STOP
storage:
  db_filename: trader.db
"""
    )

    with pytest.raises(ValueError, match="IRA accounts cannot trade crypto"):
        load_config(config_path, account_id="Roth_IRA")

    cfg = load_config(
        config_path,
        require_alpaca=False,
        account_id="Roth_IRA",
        validate_strategy=False,
    )
    assert cfg.account.id == "Roth_IRA"


def test_dashboard_write_mode_requires_password(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DASHBOARD_READ_ONLY", "false")
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
universe: [BTC/USD]
strategy:
  name: btc_sma
  params: {}
schedule:
  interval_minutes: 15
risk:
  max_position_pct: 1.0
  daily_loss_limit_pct: 0.03
  min_cash_buffer_pct: 0.02
  kill_switch_path: ./data/STOP
storage:
  db_filename: trader.db
"""
    )
    cfg = load_config(config_path, require_alpaca=False)

    with pytest.raises(RuntimeError, match="DASHBOARD_PASSWORD"):
        create_app(cfg)


def test_state_changing_post_requires_csrf(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DASHBOARD_READ_ONLY", "false")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret-pass")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
universe: [BTC/USD]
strategy:
  name: btc_sma
  params: {}
schedule:
  interval_minutes: 15
risk:
  max_position_pct: 1.0
  daily_loss_limit_pct: 0.03
  min_cash_buffer_pct: 0.02
  kill_switch_path: ./data/STOP
storage:
  db_filename: trader.db
"""
    )
    app = create_app(load_config(config_path, require_alpaca=False))
    client = TestClient(app)

    login = client.post(
        "/login",
        data={"username": "trader", "password": "secret-pass"},
        follow_redirects=False,
    )
    assert login.status_code == 303

    response = client.post("/risk/kill-switch/engage", follow_redirects=False)

    assert response.status_code == 403


def test_strategy_signal_reads_filter_to_active_strategy(tmp_path) -> None:
    db_path = tmp_path / "trader.db"
    Storage(db_path)
    with sqlite3.connect(db_path) as c:
        c.execute(
            "INSERT INTO signals (ts_utc, strategy, symbol, target_pct, rationale) "
            "VALUES (?, ?, ?, ?, ?)",
            ("2026-05-05T15:00:00+00:00", "btc_sma", "BTC/USD", 0.95, "old"),
        )
        c.execute(
            "INSERT INTO signals (ts_utc, strategy, symbol, target_pct, rationale) "
            "VALUES (?, ?, ?, ?, ?)",
            ("2026-05-05T16:00:00+00:00", "Golden Tech", "SPY", 0.2, "active"),
        )

    active = _read_signals(db_path, strategy="Golden Tech")
    latest = _read_latest_signal(db_path, "Golden Tech")

    assert [row["strategy"] for row in active] == ["Golden Tech"]
    assert latest is not None
    assert latest["symbol"] == "SPY"
