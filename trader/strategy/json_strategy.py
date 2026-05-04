"""Strategies defined by a JSON file rather than Python code.

Drop a `.json` file into this folder. On dashboard / trader restart it is
auto-discovered and registered alongside the hand-coded strategies. The
"Deploy" button on /strategy and `switch-strategy --name <name>` both work
without further code changes.

────────────────────────────────────────────────────────────────────────
RECIPE-STYLE SPEC (all fields except `name` and `type` are optional)

{
  "name": "btc_fast_sma",          // registry key; what you'll set as
                                    //   strategy.name in config.yaml
  "type": "sma_crossover",          // one of the built-in strategy classes:
                                    //   sma_crossover | btc_sma | yypt_tqqq_rsi
  "is_crypto": true,                // override the underlying class's flag
                                    //   (e.g. point sma_crossover at BTC/USD)
  "params": {                       // passed to the underlying class's __init__
    "target_symbol": "BTC/USD",
    "fast_window": 10,
    "slow_window": 30,
    "target_allocation": 0.9
  }
}

────────────────────────────────────────────────────────────────────────
WORKFLOW (on the droplet)

  1. Drop the JSON file into /opt/trader/trader/strategy/.
     Either via git (commit + push + sudo bash deploy/update.sh) or
     by SCP-ing it directly and reloading services.

  2. Restart the services so Python re-imports the strategy package:
       sudo systemctl restart trader trader-dashboard

  3. Activate it. Either:
       - Edit config.yaml's `strategy:` block, OR
       - Click "Deploy" on the dashboard's Strategy tab, OR
       - Run: trader-cli switch-strategy --name <name> --flatten --restart

The active strategy lives in `config.yaml::strategy.name`. There is no
separate "active strategy" file — `config.yaml` IS that file.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base import Strategy


def _load_json_specs() -> dict[str, dict]:
    """Scan trader/strategy/*.json and return name → spec dict.

    Malformed / unreadable files are skipped (we never want a typo'd JSON
    to crash the whole strategy registry — that would take down the bot
    AND the dashboard at startup).
    """
    specs: dict[str, dict] = {}
    json_dir = Path(__file__).parent
    for p in sorted(json_dir.glob("*.json")):
        try:
            spec = json.loads(p.read_text())
        except Exception:
            continue
        if not isinstance(spec, dict):
            continue
        name = spec.get("name") or p.stem
        spec["_source_path"] = str(p)
        specs[name] = spec
    return specs


def make_json_strategy_class(spec: dict[str, Any], builtins: dict[str, type[Strategy]]):
    """Generate a Strategy subclass for one JSON spec.

    The generated class:
      - inherits from the underlying class named in spec["type"]
      - bakes spec["params"] in as defaults; config.yaml params still
        override per-deploy
      - overrides .name and (optionally) .is_crypto from the spec
      - carries inspect-friendly metadata so the dashboard's
        Strategy Reference tab shows useful info instead of "<lambda>"
    """
    underlying_name = spec.get("type")
    if not underlying_name:
        raise ValueError(
            f"JSON strategy {spec.get('name')!r} missing required 'type' field"
        )
    if underlying_name not in builtins:
        raise ValueError(
            f"JSON strategy {spec.get('name')!r} references unknown type "
            f"{underlying_name!r}. Known types: {sorted(builtins)}"
        )
    base_cls = builtins[underlying_name]
    json_params: dict = dict(spec.get("params", {}))
    name = spec.get("name") or "unnamed_json"
    is_crypto_override = spec.get("is_crypto")

    class _JsonStrategy(base_cls):  # type: ignore[misc, valid-type]
        # The dashboard reads .name via the registry key, but having it on
        # the instance keeps logging consistent.
        pass

    def _init(self, **override_params):
        merged = {**json_params, **override_params}
        base_cls.__init__(self, **merged)
        self.name = name
        if is_crypto_override is not None:
            self.is_crypto = bool(is_crypto_override)

    _JsonStrategy.__init__ = _init
    _JsonStrategy.__name__ = f"JsonStrategy_{name}"
    _JsonStrategy.__qualname__ = _JsonStrategy.__name__
    _JsonStrategy.name = name
    if is_crypto_override is not None:
        _JsonStrategy.is_crypto = bool(is_crypto_override)
    _JsonStrategy.__doc__ = (
        f"JSON-defined strategy '{name}', extending {base_cls.__name__}.\n\n"
        f"Source: {spec.get('_source_path', '(inline)')}\n"
        f"Type:   {underlying_name}\n"
        f"Params (defaults from JSON):\n"
        + "\n".join(f"  {k}: {v!r}" for k, v in json_params.items())
        + (f"\nis_crypto: {bool(is_crypto_override)}" if is_crypto_override is not None else "")
        + "\n\n"
        + (base_cls.__doc__ or "")
    )
    # Stash for introspection (e.g. dashboard could surface this).
    _JsonStrategy._json_spec = spec  # type: ignore[attr-defined]
    _JsonStrategy._json_params = json_params  # type: ignore[attr-defined]
    return _JsonStrategy
