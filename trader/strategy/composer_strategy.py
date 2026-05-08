"""Composer-symphony JSON evaluator.

Loads a JSON tree exported from Composer and evaluates it on each tick to
produce target weights per symbol. Only the subset of the Composer DSL that
appears in user-supplied symphonies is implemented; unknown nodes log a
warning and are skipped rather than crashing the bot.

Currently supported
-------------------
Step types:
  asset                     — leaf, holds the named ticker
  root, group               — passthrough containers
  wt-cash-equal             — equal-weight allocation across children
  wt-cash-specified         — children weighted by their `weight` fraction
  if                        — branch on first matching `if-child`
  if-child                  — condition (with `lhs-fn`/comparator/`rhs-val`)
                              or "else" (no condition)

Indicator functions (in if-child conditions):
  max-drawdown(window)      — N-day max drawdown as percent (0-100 scale)

Comparators:  gt | lt | eq | gte | lte

If the loaded JSON is wrapped in RTF (TextEdit's default save format), the
wrapper is stripped automatically. To avoid this, save symphonies as plain
text JSON in the first place.

NOTE: extending the evaluator
-----------------------------
Add a new indicator: implement it in `_INDICATOR_FNS`, taking a `pd.Series`
of closes and a window int.

Add a new step type: handle it inside `_eval()`.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pandas as pd
from loguru import logger

from .base import Signal, Strategy


# ---------------------------------------------------------------------------
# RTF stripping (best-effort) and ticker normalization
# ---------------------------------------------------------------------------

def _strip_rtf(text: str) -> str:
    """Remove TextEdit-style RTF wrapping around a JSON document.

    TextEdit on macOS saves text files as RTF by default. The actual JSON
    content is wrapped in `{\\rtf1...}` with control words and brace/quote
    escapes. Re-saving as plain text in the editor is the right long-term
    fix; this is a best-effort recovery so we don't refuse to load on a
    user's first try.
    """
    if not text.startswith("{\\rtf"):
        return text
    # The first \{ marks the start of the JSON body.
    start = text.find("\\{")
    if start < 0:
        raise ValueError("Could not find JSON content inside RTF wrapper")
    body = text[start:]
    # Unescape RTF brace escapes.
    body = body.replace("\\{", "{").replace("\\}", "}")
    # RTF uses backslash+newline as a soft line break — drop them.
    body = re.sub(r"\\\s*\n", "\n", body)
    # Strip residual RTF control words like \cf0, \f0, \fs26.
    body = re.sub(r"\\[a-z]+-?\d*\s?", "", body)
    # The outer RTF group's closing `}` will be appended after the JSON body.
    # Walk the JSON forward until braces balance, then truncate.
    depth = 0
    end = -1
    for i, ch in enumerate(body):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    return body[:end] if end > 0 else body


def _normalize_ticker(t: str) -> str:
    """Composer prefixes some tickers like `EQUITIES::AIA//USD`. Reduce to `AIA`."""
    if not t:
        return t
    if "::" in t and "//" in t:
        try:
            mid = t.split("::", 1)[1]
            return mid.split("//", 1)[0]
        except Exception:
            return t
    return t


def _parse_weight(w: Any) -> float:
    """Composer weights are `{num: '3', den: 100}` fractions (0.03), or absent."""
    if w is None:
        return 0.0
    if isinstance(w, dict):
        try:
            return float(w["num"]) / float(w["den"])
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            return 0.0
    try:
        return float(w)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Indicator functions
# ---------------------------------------------------------------------------

def _max_drawdown_pct(closes: pd.Series, window: int) -> float:
    """Worst peak-to-trough drop within the last `window` bars, as percent."""
    if closes is None or len(closes) < 2:
        return 0.0
    series = closes.iloc[-window:] if window and len(closes) > window else closes
    rolling_max = series.cummax()
    drawdown = (series - rolling_max) / rolling_max * 100.0
    return float(abs(drawdown.min()))


_INDICATOR_FNS = {
    "max-drawdown": _max_drawdown_pct,
}

_COMPARATORS = {
    "gt":  lambda a, b: a > b,
    "lt":  lambda a, b: a < b,
    "eq":  lambda a, b: a == b,
    "gte": lambda a, b: a >= b,
    "lte": lambda a, b: a <= b,
}


# ---------------------------------------------------------------------------
# Strategy class
# ---------------------------------------------------------------------------

class ComposerStrategy(Strategy):
    name = "composer"
    is_crypto = False

    def __init__(
        self,
        path: str | Path | None = None,
        spec: dict | None = None,
        name: str | None = None,
        target_allocation: float = 0.95,
        cash_fallback_symbol: str | None = None,
    ):
        """Construct from either a file path or an in-memory spec dict.

        target_allocation scales every leaf weight by this factor — Composer
        symphonies typically allocate to 100% but our risk manager keeps a
        cash buffer (default 2%), so 0.95 is a safe default.
        cash_fallback_symbol, when set, receives any unallocated Composer cash
        sleeve inside target_allocation.
        """
        if spec is None:
            if path is None:
                raise ValueError("Either path or spec must be provided")
            text = Path(path).read_text()
            text = _strip_rtf(text)
            spec = json.loads(text)
        if not (0.0 < target_allocation <= 1.0):
            raise ValueError("target_allocation must be in (0, 1]")
        self._spec = spec
        self.name = name or spec.get("name") or "composer"
        self._target_allocation = target_allocation
        self._cash_fallback_symbol = (
            _normalize_ticker(cash_fallback_symbol) if cash_fallback_symbol else None
        )
        self._symbols = self._collect_symbols(spec)
        if self._cash_fallback_symbol and self._cash_fallback_symbol not in self._symbols:
            self._symbols.append(self._cash_fallback_symbol)
        self.is_crypto = False  # Composer uses equity tickers via Alpaca stock feed

    @property
    def universe(self) -> list[str]:
        return self._symbols

    # --- traversal helpers ---

    def _collect_symbols(self, node: Any) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()

        def walk(n: Any) -> None:
            if not isinstance(n, dict):
                return
            if n.get("step") == "asset":
                t = _normalize_ticker(n.get("ticker", ""))
                if t and t not in seen:
                    seen.add(t)
                    out.append(t)
                return
            for child in n.get("children") or []:
                walk(child)

        walk(node)
        return out

    # --- public compute entrypoint ---

    def compute(self, bars: dict[str, pd.DataFrame]) -> list[Signal]:
        weights: dict[str, float] = {sym: 0.0 for sym in self._symbols}
        try:
            self._eval(self._spec, bars, ratio=self._target_allocation, weights=weights)
        except Exception as e:
            logger.error(f"composer eval failed for {self.name}: {e}")
            return [Signal(s, 0.0, "composer eval error") for s in self._symbols]

        if self._cash_fallback_symbol:
            invested = sum(weights.values())
            residual = max(0.0, self._target_allocation - invested)
            if residual > 0:
                weights[self._cash_fallback_symbol] = (
                    weights.get(self._cash_fallback_symbol, 0.0) + residual
                )

        signals: list[Signal] = []
        for sym, w in weights.items():
            w = round(w, 6)
            if w > 0:
                rationale = (
                    f"cash fallback:{self.name}"
                    if sym == self._cash_fallback_symbol
                    else f"composer:{self.name}"
                )
                signals.append(Signal(sym, w, rationale))
            else:
                signals.append(Signal(sym, 0.0, "flat (composer)"))
        return signals

    # --- recursive evaluator ---

    def _eval(
        self,
        node: Any,
        bars: dict[str, pd.DataFrame],
        ratio: float,
        weights: dict[str, float],
    ) -> None:
        if not isinstance(node, dict):
            return
        step = node.get("step")
        children = node.get("children") or []

        if step == "asset":
            sym = _normalize_ticker(node.get("ticker", ""))
            if sym:
                weights[sym] = weights.get(sym, 0.0) + ratio
            return

        if step in ("root", "group", None):
            for child in children:
                self._eval(child, bars, ratio, weights)
            return

        if step == "wt-cash-equal":
            if not children:
                return
            child_ratio = ratio / len(children)
            for child in children:
                self._eval(child, bars, child_ratio, weights)
            return

        if step == "wt-cash-specified":
            for child in children:
                w = _parse_weight(child.get("weight"))
                self._eval(child, bars, ratio * w, weights)
            return

        if step == "if":
            # Composer "if" picks the first if-child with a true condition;
            # if none match, the if-child without a condition is the "else".
            else_child = None
            for child in children:
                if child.get("step") != "if-child":
                    continue
                if "lhs-fn" not in child:
                    if else_child is None:
                        else_child = child
                    continue
                if self._eval_condition(child, bars):
                    for grand in child.get("children") or []:
                        self._eval(grand, bars, ratio, weights)
                    return
            if else_child is not None:
                for grand in else_child.get("children") or []:
                    self._eval(grand, bars, ratio, weights)
            return

        logger.warning(f"composer: unsupported step {step!r}, skipping subtree")

    def _eval_condition(self, node: dict, bars: dict[str, pd.DataFrame]) -> bool:
        fn = node.get("lhs-fn")
        sym = _normalize_ticker(node.get("lhs-val", ""))
        params = node.get("lhs-fn-params") or {}
        comparator = node.get("comparator")
        rhs = node.get("rhs-val")

        if fn not in _INDICATOR_FNS:
            logger.warning(f"composer: unsupported indicator {fn!r}; treating condition as False")
            return False
        if comparator not in _COMPARATORS:
            logger.warning(f"composer: unsupported comparator {comparator!r}; condition False")
            return False

        df = bars.get(sym)
        if df is None or df.empty or "close" not in df.columns:
            logger.debug(f"composer: no bars for {sym}; condition False")
            return False

        try:
            window = int(params.get("window", 30))
        except (TypeError, ValueError):
            window = 30

        try:
            lhs_val = _INDICATOR_FNS[fn](df["close"], window)
            rhs_val = float(rhs)
        except Exception as e:
            logger.warning(f"composer: condition eval error: {e}")
            return False

        return _COMPARATORS[comparator](lhs_val, rhs_val)
