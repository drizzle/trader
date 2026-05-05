"""Strategy registry.

Two ways to add a strategy:

  1. Hand-code a class in this folder, import it below, and add it to
     `_BUILTIN`. Use this when the strategy needs custom Python logic
     (loops, indicators not in the helpers, etc.).

  2. Drop a `.json` file into this folder. It will be auto-discovered at
     startup and registered alongside the built-ins. See
     `json_strategy.py` for the schema. Use this when you just want
     to vary parameters of an existing class (windows, symbol, allocation)
     without writing Python.

The combined registry is exposed via `STRATEGIES` and `build_strategy()`,
both of which the dashboard's deploy flow and the trader bot rely on.
"""
from __future__ import annotations

from loguru import logger

from .base import Signal, Strategy
from .btc_sma import BtcSmaStrategy
from .json_strategy import _load_json_specs, make_json_strategy_class
from .sma_crossover import SmaCrossoverStrategy
from .yypt_tqqq_rsi import YyptTqqqRsiStrategy


# Hand-coded strategies. The JSON layer extends this dict; it does NOT
# replace it.
_BUILTIN: dict[str, type[Strategy]] = {
    "sma_crossover":   SmaCrossoverStrategy,
    "yypt_tqqq_rsi":   YyptTqqqRsiStrategy,
    "btc_sma":         BtcSmaStrategy,
}

# Combined registry: built-ins + every valid JSON spec found alongside.
STRATEGIES: dict[str, type[Strategy]] = {}


def reload_json_strategies() -> dict[str, type[Strategy]]:
    """Refresh the combined registry after Composer JSON imports."""
    STRATEGIES.clear()
    STRATEGIES.update(_BUILTIN)
    for _name, _spec in _load_json_specs().items():
        if _name in STRATEGIES:
            logger.warning(
                f"JSON strategy '{_name}' shadows a built-in; ignoring the JSON. "
                f"Rename the JSON's 'name' field to register it separately."
            )
            continue
        try:
            STRATEGIES[_name] = make_json_strategy_class(_spec, _BUILTIN)
        except Exception as e:
            logger.warning(
                f"Failed to register JSON strategy '{_name}' "
                f"({_spec.get('_source_path', '?')}): {e}"
            )
    return STRATEGIES


reload_json_strategies()


def build_strategy(name: str, params: dict) -> Strategy:
    if name not in STRATEGIES:
        raise ValueError(
            f"Unknown strategy '{name}'. Available: {list(STRATEGIES.keys())}"
        )
    return STRATEGIES[name](**params)


__all__ = ["Signal", "Strategy", "build_strategy", "STRATEGIES", "reload_json_strategies"]
