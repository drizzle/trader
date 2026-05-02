"""Strategy registry. Add new strategies here so they can be selected via config.yaml."""
from __future__ import annotations

from .base import Signal, Strategy
from .sma_crossover import SmaCrossoverStrategy

# Register strategies by their config name.
STRATEGIES: dict[str, type[Strategy]] = {
    "sma_crossover": SmaCrossoverStrategy,
}


def build_strategy(name: str, params: dict) -> Strategy:
    if name not in STRATEGIES:
        raise ValueError(
            f"Unknown strategy '{name}'. Available: {list(STRATEGIES.keys())}"
        )
    return STRATEGIES[name](**params)


__all__ = ["Signal", "Strategy", "build_strategy", "STRATEGIES"]
