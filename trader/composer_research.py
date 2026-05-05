"""Composer-style research, imports, and trader-manager reports.

This module deliberately avoids relying on an unofficial Composer API. The
public Composer site is useful for ideas and mental models, but durable
automation comes from imported symphony JSON, local backtests, broker snapshots,
and optional LLM synthesis.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from .advisors.llm import DeepSeekClient, LLMError
from .backtest import run_backtest
from .config import Config
from .data import DataClient
from .storage import Storage
from .strategy import STRATEGIES, build_strategy, reload_json_strategies
from .strategy.composer_strategy import _strip_rtf
from .strategy.json_strategy import _load_json_specs


def safe_slug(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", name.strip()).strip("._")
    return slug[:80] or "composer_import"


def import_dir(cfg: Config) -> Path:
    path = cfg.data_dir / "composer_imports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def manager_dir(cfg: Config) -> Path:
    path = cfg.data_dir / "manager_reports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_composer_json(text: str, fallback_name: str = "composer_import") -> dict[str, Any]:
    text = _strip_rtf(text.strip())
    spec = json.loads(text)
    if not isinstance(spec, dict):
        raise ValueError("Composer import must be a JSON object")
    spec.setdefault("name", fallback_name)
    spec.setdefault("imported_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    return spec


def save_composer_import(cfg: Config, text: str, requested_name: str | None = None) -> Path:
    fallback = safe_slug(requested_name or "composer_import")
    spec = parse_composer_json(text, fallback)
    name = safe_slug(requested_name or spec.get("name") or fallback)
    spec["name"] = name
    path = import_dir(cfg) / f"{name}.json"
    path.write_text(json.dumps(spec, indent=2, sort_keys=False))
    reload_json_strategies()
    return path


def _walk_nodes(node: Any):
    if isinstance(node, dict):
        yield node
        for child in node.get("children") or []:
            yield from _walk_nodes(child)
    elif isinstance(node, list):
        for child in node:
            yield from _walk_nodes(child)


def _strategy_assets(cls: type) -> list[str]:
    try:
        inst = cls()
        return list(inst.universe)
    except Exception:
        spec = getattr(cls, "_json_spec", {}) or {}
        out = []
        seen = set()
        for node in _walk_nodes(spec):
            if isinstance(node, dict) and node.get("step") == "asset":
                ticker = str(node.get("ticker") or "")
                if "::" in ticker and "//" in ticker:
                    ticker = ticker.split("::", 1)[1].split("//", 1)[0]
                if ticker and ticker not in seen:
                    seen.add(ticker)
                    out.append(ticker)
        return out


def list_composer_strategies() -> list[dict[str, Any]]:
    reload_json_strategies()
    specs = _load_json_specs()
    rows = []
    for name, cls in sorted(STRATEGIES.items()):
        spec = specs.get(name) or getattr(cls, "_json_spec", None)
        if not spec:
            continue
        assets = _strategy_assets(cls)
        rows.append({
            "name": name,
            "source_path": (spec or {}).get("_source_path", ""),
            "asset_count": len(assets),
            "assets_preview": assets[:12],
            "is_composer": bool((spec or {}).get("children") or (spec or {}).get("step")),
        })
    return rows


def _score_metrics(metrics: dict[str, Any]) -> float:
    cagr = float(metrics.get("cagr_pct") or 0)
    sharpe = float(metrics.get("sharpe") or 0)
    drawdown = abs(float(metrics.get("max_drawdown_pct") or 0))
    trades = float(metrics.get("n_trades") or 0)
    return round(cagr + sharpe * 8 - drawdown * 0.35 + min(trades, 80) * 0.05, 2)


def scan_imported_strategies(
    cfg: Config,
    *,
    years: int = 3,
    limit: int = 12,
) -> dict[str, Any]:
    """Backtest imported Composer strategies and rank them by return/risk."""
    data = DataClient(cfg.alpaca)
    rows = []
    errors = []
    end = datetime.now(timezone.utc).date()
    start = end.replace(year=end.year - years)
    for item in list_composer_strategies()[:limit]:
        name = item["name"]
        try:
            strategy = build_strategy(name, {})
            bars = data.bars_for_universe(strategy.universe, lookback_days=years * 365 + 80)
            result = run_backtest(
                strategy,
                bars,
                initial_cash=100_000,
                risk_config=cfg.risk,
                start=start.isoformat(),
                end=end.isoformat(),
                slippage_bps=5.0,
            )
            metrics = result.metrics
            rows.append({
                **item,
                "metrics": metrics,
                "score": _score_metrics(metrics),
            })
        except Exception as e:
            errors.append({"name": name, "error": str(e)})
    rows.sort(key=lambda r: r.get("score", -math.inf), reverse=True)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "years": years,
        "rows": rows,
        "errors": errors,
    }


def save_composer_scan(cfg: Config, scan: dict[str, Any]) -> Path:
    path = cfg.data_dir / "composer_scan.json"
    path.write_text(json.dumps(scan, indent=2, sort_keys=False))
    return path


def load_composer_scan(cfg: Config) -> dict[str, Any] | None:
    path = cfg.data_dir / "composer_scan.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _latest_manager_report(cfg: Config) -> dict[str, Any] | None:
    reports = sorted(manager_dir(cfg).glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not reports:
        return None
    try:
        return json.loads(reports[0].read_text())
    except Exception:
        return None


def latest_manager_report(cfg: Config) -> dict[str, Any] | None:
    return _latest_manager_report(cfg)


def _portfolio_snapshot(cfg: Config) -> dict[str, Any]:
    storage = Storage(cfg.db_path)
    latest_position_ts, positions = storage.latest_position_snapshot()
    latest_equity = None
    if cfg.db_path.exists():
        import sqlite3

        with sqlite3.connect(cfg.db_path) as c:
            c.row_factory = sqlite3.Row
            row = c.execute(
                "SELECT * FROM equity_snapshots ORDER BY ts_utc DESC LIMIT 1"
            ).fetchone()
            latest_equity = dict(row) if row else None
    return {
        "account": cfg.account.id,
        "strategy": cfg.strategy.name,
        "risk": cfg.risk.model_dump(),
        "latest_equity": latest_equity,
        "latest_position_ts": latest_position_ts,
        "positions": positions,
    }


def generate_manager_report(cfg: Config, cadence: str = "daily") -> dict[str, Any]:
    scan = load_composer_scan(cfg)
    snapshot = _portfolio_snapshot(cfg)
    prompt_payload = {
        "cadence": cadence,
        "portfolio": snapshot,
        "composer_scan": scan,
        "question": (
            "Challenge the strategy process. Identify return drivers, risk concentrations, "
            "cash-buffer compliance, benchmark-relative performance, and concrete next steps. "
            "Do not recommend automatic deployment. Separate observations from hypotheses."
        ),
    }
    synthesis = "LLM not configured. Review imported Composer ranks, cash buffer, concentration, and drawdowns manually."
    if cfg.deepseek.enabled:
        client = DeepSeekClient(cfg.deepseek.api_key or "")
        try:
            synthesis = client.chat(
                system=(
                    "You are a skeptical trader-manager agent. Be concise, risk-aware, "
                    "macro-aware, and specific. Past performance is not evidence of edge."
                ),
                user=json.dumps(prompt_payload, indent=2, default=str),
                max_tokens=1200,
                temperature=0.25,
            )
        except LLMError as e:
            logger.warning(f"manager LLM synthesis failed: {e}")
            synthesis = f"LLM synthesis failed: {e}"
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cadence": cadence,
        "portfolio": snapshot,
        "composer_scan": scan,
        "synthesis": synthesis,
    }
    path = manager_dir(cfg) / f"{cadence}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(report, indent=2, default=str))
    return report
