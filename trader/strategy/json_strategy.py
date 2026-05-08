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
import os
from pathlib import Path
from typing import Any

from .base import Strategy
from .composer_strategy import ComposerStrategy, _strip_rtf


def _json_search_dirs() -> list[Path]:
    dirs = [Path(__file__).parent]
    env_dir = os.environ.get("COMPOSER_IMPORT_DIR")
    if env_dir:
        dirs.append(Path(env_dir))
    data_root = Path(os.environ.get("DATA_DIR", "./data"))
    dirs.append(data_root / "composer_imports")
    if data_root.exists():
        dirs.extend(sorted(p for p in data_root.glob("*/composer_imports") if p.is_dir()))
    out = []
    seen = set()
    for d in dirs:
        try:
            resolved = d.resolve()
        except Exception:
            resolved = d
        if resolved not in seen:
            seen.add(resolved)
            out.append(d)
    return out


def _load_json_specs() -> dict[str, dict]:
    """Scan trader/strategy/*.json (and .JSON) and return name → spec dict.

    Auto-strips TextEdit-style RTF wrappers so users don't have to remember
    to "Save as plain text". Malformed / unreadable files are skipped — a
    typo'd JSON must NEVER take down the bot or the dashboard at startup.
    """
    specs: dict[str, dict] = {}
    seen_paths: set[Path] = set()
    for json_dir in _json_search_dirs():
        if not json_dir.exists():
            continue
        for p in sorted(list(json_dir.glob("*.json")) + list(json_dir.glob("*.JSON"))):
            if p in seen_paths:
                continue
            seen_paths.add(p)
            try:
                text = p.read_text()
                if text.startswith("{\\rtf"):
                    text = _strip_rtf(text)
                spec = json.loads(text)
            except Exception:
                continue
            if not isinstance(spec, dict):
                continue
            name = spec.get("name") or p.stem
            spec["_source_path"] = str(p)
            specs[name] = spec
    return specs


def _is_composer_spec(spec: dict) -> bool:
    """Composer symphonies have nested children with `step` fields.

    The recipe format we also support has a flat `{name, type, params}` shape
    with no `children` and no `step`. This is enough to disambiguate cleanly.
    """
    if not isinstance(spec, dict):
        return False
    if spec.get("step") in {"root", "group", "if", "wt-cash-equal", "wt-cash-specified"}:
        return True
    children = spec.get("children")
    if isinstance(children, list):
        return any(isinstance(c, dict) and "step" in c for c in children)
    return False


def make_json_strategy_class(spec: dict[str, Any], builtins: dict[str, type[Strategy]]):
    """Generate a Strategy subclass for one JSON spec.

    Two formats are supported:

      1. Composer-symphony JSON  — detected by nested `children`/`step`
         fields. Wrapped as a ComposerStrategy.
      2. Recipe JSON             — `{name, type, params, is_crypto}` form.
         Wraps an existing built-in strategy class with custom defaults.

    The generated class carries inspect-friendly metadata so the dashboard's
    Strategy Reference tab shows useful info instead of "<lambda>".
    """
    if _is_composer_spec(spec):
        return _make_composer_class(spec)

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


def _make_composer_class(spec: dict):
    """Wrap a Composer symphony spec as a ComposerStrategy subclass.

    Adds a kwarg-tolerant __init__ so the registry can call it with the
    same `(**params)` shape it uses for all other strategies. config.yaml's
    `strategy.params.target_allocation` (if any) is honored. Golden Tech also
    maps unallocated Composer cash to SGOV unless explicitly overridden.
    """
    name = spec.get("name") or "composer_unnamed"
    source_path = spec.get("_source_path", "(inline)")
    asset_count = sum(
        1 for n in _walk_nodes(spec) if isinstance(n, dict) and n.get("step") == "asset"
    )
    universe_preview = []
    for n in _walk_nodes(spec):
        if isinstance(n, dict) and n.get("step") == "asset":
            t = n.get("ticker", "")
            if t and t not in universe_preview:
                universe_preview.append(t)
                if len(universe_preview) >= 8:
                    break

    class _ComposerWrapped(ComposerStrategy):
        pass

    def _init(
        self,
        target_allocation: float = 0.95,
        cash_fallback_symbol: str | None = None,
        **ignored,
    ):
        if cash_fallback_symbol is None and name == "Golden Tech":
            cash_fallback_symbol = "SGOV"
        ComposerStrategy.__init__(
            self,
            spec=spec,
            name=name,
            target_allocation=target_allocation,
            cash_fallback_symbol=cash_fallback_symbol,
        )

    _ComposerWrapped.__init__ = _init
    _ComposerWrapped.__name__ = f"ComposerStrategy_{name}"
    _ComposerWrapped.__qualname__ = _ComposerWrapped.__name__
    _ComposerWrapped.name = name
    _ComposerWrapped.is_crypto = False
    _ComposerWrapped.__doc__ = (
        f"Composer-symphony strategy '{name}'.\n\n"
        f"Source: {source_path}\n"
        f"Total asset leaves: {asset_count}\n"
        f"First tickers: {', '.join(universe_preview)}"
        f"{'…' if asset_count > len(universe_preview) else ''}\n\n"
        + (ComposerStrategy.__doc__ or "")
    )
    _ComposerWrapped._json_spec = spec  # type: ignore[attr-defined]
    return _ComposerWrapped


def _walk_nodes(node):
    """Generator: yield every dict node in the tree (including the root)."""
    if isinstance(node, dict):
        yield node
        for child in node.get("children") or []:
            yield from _walk_nodes(child)
    elif isinstance(node, list):
        for child in node:
            yield from _walk_nodes(child)
