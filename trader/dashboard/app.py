"""FastAPI dashboard.

Five views:
  /            → system health, strategy config, and strategy reference
  /trades      → positions + recent fills + equity curve since deployment
  /risk        → all risk caps and current exposures vs limits
  /backtests   → list of HTML reports under data/backtests/
  /advisors    → read-only advisory output from external advisor services

Read-only by default. Binds to 127.0.0.1 by default — access via SSH tunnel:
    ssh -L 8000:localhost:8000 root@<droplet-ip>
    open http://localhost:8000
"""
from __future__ import annotations

import os
import base64
import inspect
import json
import resource
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from ..advisors import (
    DeepSeekClient, LLMError,
    build_default_advisors,
)
from ..advisors.cache import RecommendationCache
from ..advisors.context import build_context
from ..config import Config
from ..data import DataClient
from ..execution import ExecutionClient
from ..market_summary import (
    PROMPTS, PROMPTS_BY_KEY, build_user_prompt as build_summary_user_prompt,
    run_summary,
)
from ..storage import Storage
from ..strategy import STRATEGIES

def _safe_b64_decode(b64_str: str) -> bytes | None:
    """Decode base64, auto-padding to a multiple of 4. Returns None on failure.

    Defensive: a malformed embedded asset must NEVER take down the dashboard.
    """
    try:
        s = "".join(b64_str.split())  # strip all whitespace/newlines
        s += "=" * (-len(s) % 4)      # restore missing padding
        return base64.b64decode(s)
    except Exception:
        return None


_APPLE_TOUCH_ICON_PNG = _safe_b64_decode(
    "iVBORw0KGgoAAAANSUhEUgAAALQAAAC0CAYAAAA9zQYyAAACWklEQVR42u3doRWEQBBEQRI4geEEjqiIkNBwKALg/CkEj9c7W+IH0EzpZfiM30uq0uAjCGgJaAloCWgBLQEtAS0BLQEtoKXOQK/bJb0W0AIaaAEtAS0BLaCBFtBAC+j/pnmRbge0gAZaQAMtoIEW0AIaaAENtIAGWkBLQAtooAU00AIaaAEtAS2ggRbQQAtooAW0BLSABlpAAy2ggRbQEtACGmgBDbSATgC9H2dU9gLtwEADDTTQQAMNNNBAAw20vUA7MNBAAw000EADDTTQQANtL9BAA10JdBqgNHCt7wUaaKCBBhpooIEGGmiggQYaaKCBBhpooIEGGmiggQYaaKCBBhpooIFOrre9QAMNNNBAAw000EADbS/QDgw00EADDTTQQAMNNNBA2wu0AwMNNNBAAw000EADDTTQ9gINNNBAAw000EADDTTQQDuwvUADDTTQQAMNNNBAAw000A5sL9BAAw000EADDTTQQAMNtAPbCzTQQAMNNNBAAw000EAD7cD2Ag000EC3BdRPg4AGmiggQYaaKCBBhpooIEGGmiggQYaaKCBBhpooIEGGmiggQYa6GTQaYDSwFXbCzTQQAMNNNBAAw000PYC7cBAAw000EADDTTQQAMNtL1AOzDQQAMNdC3Q8tAM0AIaaAEtAS2ggRbQQAtooAW0BLSABlpAAy2ggRbQEtACGmgBDbSABlpAS0ALaKAFNNACGmgBLQEtoIEW0EALaKBVD7T0ZEALaKAFtAS0BLSABlpAA63+QEvJAS2gJaAloCWgBbQEtAS0BLQEtICWGu8HpCQRe+zufA4AAAAASUVORK5CYII="
)


def _read_signals(db_path: Path, limit: int = 50) -> list[dict]:
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT ts_utc, strategy, symbol, target_pct, rationale "
            "FROM signals ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def _read_orders(db_path: Path, limit: int = 50) -> list[dict]:
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT ts_utc, symbol, side, qty, status, filled_qty, "
            "filled_avg_price, error, strategy "
            "FROM orders ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def _read_equity_curve(db_path: Path) -> list[dict]:
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT ts_utc, equity, cash FROM equity_snapshots ORDER BY ts_utc ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def _read_latest_row(db_path: Path, table: str) -> dict | None:
    if table not in {"signals", "orders", "equity_snapshots"} or not db_path.exists():
        return None
    with sqlite3.connect(db_path) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(f"SELECT * FROM {table} ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None


def _read_advisor_json(cfg: Config) -> tuple[dict | None, Path]:
    path = Path(
        os.environ.get(
            "AI_HEDGE_FUND_ADVICE_PATH",
            str(cfg.data_dir / "advisors" / "ai_hedge_fund.json"),
        )
    )
    if not path.exists():
        return None, path
    try:
        return json.loads(path.read_text()), path
    except json.JSONDecodeError:
        return {"error": "Advisor JSON file is not valid JSON."}, path


def _normalize_decision(raw: object) -> dict:
    if isinstance(raw, str):
        return {"action": raw.upper(), "confidence": None, "reasoning": ""}
    if not isinstance(raw, dict):
        return {"action": "NO DATA", "confidence": None, "reasoning": ""}

    action = (
        raw.get("action")
        or raw.get("signal")
        or raw.get("decision")
        or raw.get("recommendation")
        or "NO DATA"
    )
    confidence = raw.get("confidence")
    reasoning = raw.get("reasoning") or raw.get("rationale") or raw.get("reason") or ""
    quantity = raw.get("quantity")
    return {
        "action": str(action).upper(),
        "confidence": confidence,
        "reasoning": reasoning,
        "quantity": quantity,
    }


def _action_tone(action: str) -> str:
    a = (action or "").upper()
    if a in {"BUY", "LONG", "COVER"}:
        return "buy"
    if a in {"SELL", "SHORT"}:
        return "sell"
    if a in {"HOLD", "NEUTRAL"}:
        return "hold"
    return "nodata"


def _advisor_state(cfg: Config) -> dict:
    raw, path = _read_advisor_json(cfg)
    generated_at = None
    decisions_raw = {}
    analyst_signals = {}
    advisor_matrix_raw = {}
    error = None

    if raw is None:
        error = (
            "No advisor output found yet. Click 'Refresh now' below to generate "
            "recommendations using the configured LLM."
            if cfg.deepseek.enabled
            else "No advisor output found, and DEEPSEEK_API_KEY is not configured."
        )
    elif raw.get("error"):
        error = raw["error"]
    else:
        generated_at = raw.get("generated_at") or raw.get("timestamp") or raw.get("as_of")
        decisions_raw = raw.get("decisions") or raw.get("portfolio_decisions") or {}
        analyst_signals = raw.get("analyst_signals") or {}
        advisor_matrix_raw = raw.get("advisor_recommendations") or {}

    # Aggregated row per symbol (consensus / single decision) — keeps the existing template happy.
    rows = []
    for symbol in cfg.universe:
        raw_decision = decisions_raw.get(symbol) if isinstance(decisions_raw, dict) else None
        decision = _normalize_decision(raw_decision)
        rows.append({
            "symbol": symbol,
            "action": decision["action"],
            "tone": _action_tone(decision["action"]),
            "confidence": decision["confidence"],
            "quantity": decision.get("quantity"),
            "reasoning": decision["reasoning"],
        })

    # Per-advisor matrix: rows=symbols, cols=advisors. Used by the new view.
    all_advisors: list[str] = []
    if isinstance(advisor_matrix_raw, dict):
        seen: set[str] = set()
        for sym in cfg.universe:
            for r in advisor_matrix_raw.get(sym, []) or []:
                name = r.get("advisor")
                if name and name not in seen:
                    seen.add(name); all_advisors.append(name)

    matrix_rows = []
    for sym in cfg.universe:
        per_symbol = advisor_matrix_raw.get(sym, []) if isinstance(advisor_matrix_raw, dict) else []
        by_advisor = {r.get("advisor"): r for r in per_symbol if isinstance(r, dict)}
        cells = []
        for adv in all_advisors:
            r = by_advisor.get(adv)
            if not r:
                cells.append({"advisor": adv, "action": "—", "tone": "nodata",
                              "confidence": None, "rationale": ""})
            else:
                cells.append({
                    "advisor": adv,
                    "action": (r.get("action") or "—").upper(),
                    "tone": _action_tone(r.get("action") or ""),
                    "confidence": r.get("confidence"),
                    "rationale": (r.get("rationale") or r.get("reasoning") or "").strip(),
                })
        matrix_rows.append({"symbol": sym, "cells": cells})

    age_label, age_seconds = _age_label(generated_at)
    status = "missing" if error else ("stale" if age_seconds and age_seconds > 3600 * 24 else "ok")
    return {
        "service": "AI Hedge Fund (local LLM advisors)",
        "repo_url": "https://github.com/virattt/ai-hedge-fund",
        "path": str(path),
        "generated_at": generated_at,
        "age_label": age_label,
        "status": status,
        "error": error,
        "rows": rows,
        "analyst_signals": analyst_signals,
        "advisors": all_advisors,
        "matrix_rows": matrix_rows,
        "deepseek_configured": cfg.deepseek.enabled,
    }


def _available_strategies(active_name: str) -> list[dict]:
    strategies = []
    for name, strategy_cls in sorted(STRATEGIES.items()):
        try:
            strategy = strategy_cls()
            universe = strategy.universe
        except Exception:
            universe = []
        strategies.append({
            "name": name,
            "class_name": strategy_cls.__name__,
            "universe": universe,
            "active": name == active_name,
        })
    return strategies


def _strategy_details(selected_name: str, active_name: str, active_params: dict) -> dict:
    strategy_cls = STRATEGIES.get(selected_name) or STRATEGIES[active_name]
    name = selected_name if selected_name in STRATEGIES else active_name
    signature = inspect.signature(strategy_cls.__init__)
    params = {}
    for param_name, param in signature.parameters.items():
        if param_name == "self":
            continue
        if param.default is not inspect.Parameter.empty:
            params[param_name] = param.default
    if name == active_name:
        params.update(active_params)
    try:
        strategy = strategy_cls(**params)
        universe = strategy.universe
    except Exception:
        universe = []
    module = inspect.getmodule(strategy_cls)
    logic = (
        inspect.getdoc(strategy_cls)
        or (inspect.getdoc(module) if module else None)
        or "No strategy logic description available."
    )
    return {
        "name": name,
        "class_name": strategy_cls.__name__,
        "universe": universe,
        "params": params,
        "logic": logic,
        "active": name == active_name,
    }


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _age_label(ts: str | None) -> tuple[str, float | None]:
    parsed = _parse_ts(ts)
    if parsed is None:
        return "n/a", None
    seconds = max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())
    if seconds < 90:
        return f"{seconds:.0f}s ago", seconds
    minutes = seconds / 60
    if minutes < 90:
        return f"{minutes:.0f}m ago", seconds
    hours = minutes / 60
    if hours < 48:
        return f"{hours:.1f}h ago", seconds
    return f"{hours / 24:.1f}d ago", seconds


def _memory_mb() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if usage > 10_000_000:
        return usage / (1024 * 1024)
    return usage / 1024


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.lower() in {"1", "true", "yes", "on"}


def _login_page(error: str = "") -> HTMLResponse:
    error_html = (
        f"<div class='error'>{error}</div>"
        if error
        else ""
    )
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="manifest" href="/manifest.webmanifest">
<title>trader · login</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; min-height: 100vh; display: grid; place-items: center;
         background: #0e1116; color: #e6edf3;
         font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }}
  form {{ width: min(360px, calc(100vw - 32px)); background: #161b22;
          border: 1px solid #30363d; border-radius: 8px; padding: 20px; }}
  h1 {{ margin: 0 0 16px; font-size: 1.25rem; }}
  label {{ display: block; margin: 12px 0 6px; color: #8b949e; font-size: 0.9rem; }}
  input {{ width: 100%; padding: 12px; border-radius: 6px; border: 1px solid #30363d;
           background: #0d1117; color: #e6edf3; font-size: 1rem; }}
  button {{ width: 100%; margin-top: 16px; padding: 12px; border: 0; border-radius: 6px;
            background: #58a6ff; color: #0d1117; font-size: 1rem; font-weight: 700; }}
  .error {{ background: #f8514933; color: #ffb3ad; border: 1px solid #f8514966;
            border-radius: 6px; padding: 10px; margin-bottom: 12px; }}
</style>
</head>
<body>
  <form method="post" action="/login" autocomplete="off">
    <h1>trader dashboard</h1>
    {error_html}
    <label for="username">Username</label>
    <input id="username" name="username" value="trader" autocomplete="off" autocapitalize="none" required>
    <label for="password">Password</label>
    <input id="password" name="password" type="password" autocomplete="off" required>
    <button type="submit">Sign in</button>
  </form>
</body>
</html>""", headers={"Cache-Control": "no-store"})


def create_app(cfg: Config) -> FastAPI:
    dashboard_user = os.environ.get("DASHBOARD_USERNAME", "trader")
    dashboard_password = os.environ.get("DASHBOARD_PASSWORD")
    require_auth = _env_bool("DASHBOARD_REQUIRE_AUTH", bool(dashboard_password))
    read_only = _env_bool("DASHBOARD_READ_ONLY", True)
    session_seconds = int(os.environ.get("DASHBOARD_SESSION_SECONDS", "300"))
    sessions: dict[str, datetime] = {}

    def _authenticated(request: Request) -> bool:
        if not require_auth:
            return True
        if not dashboard_password:
            return False
        token = request.cookies.get("trader_session")
        if not token:
            return False
        now = datetime.now(timezone.utc)
        last_seen = sessions.get(token)
        if last_seen is None or now - last_seen > timedelta(seconds=session_seconds):
            sessions.pop(token, None)
            return False
        sessions[token] = now
        return True

    app = FastAPI(title="trader dashboard")

    # Templates live next to this file.
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

    # We instantiate ExecutionClient lazily on each request because a brief
    # Alpaca outage shouldn't bring the dashboard down at startup.
    def _execution() -> ExecutionClient | None:
        try:
            return ExecutionClient(cfg.alpaca, Storage(cfg.db_path))
        except Exception:
            return None

    @app.middleware("http")
    async def _auth_and_no_cache(request: Request, call_next):
        path = request.url.path
        public_paths = {
            "/login", "/logout", "/healthz", "/favicon.svg",
            "/apple-touch-icon.png", "/manifest.webmanifest",
        }
        if path not in public_paths and not _authenticated(request):
            return RedirectResponse(url="/login", status_code=303)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/login", response_class=HTMLResponse)
    def login_view():
        return _login_page()

    @app.post("/login")
    async def login_submit(request: Request):
        body = (await request.body()).decode()
        form = parse_qs(body)
        username = form.get("username", [""])[0]
        password = form.get("password", [""])[0]
        user_ok = secrets.compare_digest(username, dashboard_user)
        password_ok = bool(dashboard_password) and secrets.compare_digest(
            password, dashboard_password
        )
        if not (user_ok and password_ok):
            return _login_page("Invalid username or password.")
        token = secrets.token_urlsafe(32)
        sessions[token] = datetime.now(timezone.utc)
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(
            "trader_session",
            token,
            max_age=session_seconds,
            httponly=True,
            secure=True,
            samesite="lax",
        )
        return response

    @app.get("/logout")
    def logout(request: Request):
        token = request.cookies.get("trader_session")
        if token:
            sessions.pop(token, None)
        response = RedirectResponse(url="/login", status_code=303)
        response.delete_cookie("trader_session")
        return response

    @app.get("/favicon.svg")
    def favicon():
        return HTMLResponse(
            """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 180 180">
<rect width="180" height="180" rx="36" fill="#0d1117"/>
<rect x="10" y="10" width="160" height="160" rx="30" fill="#161b22" stroke="#58a6ff" stroke-width="8"/>
<text x="90" y="108" text-anchor="middle" font-family="-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif" font-size="64" font-weight="800" fill="#e6edf3">CC</text>
</svg>""",
            media_type="image/svg+xml",
        )

    @app.get("/apple-touch-icon.png")
    def apple_touch_icon():
        return Response(_APPLE_TOUCH_ICON_PNG, media_type="image/png")

    @app.get("/manifest.webmanifest")
    def manifest():
        return JSONResponse({
            "name": "CC Trader",
            "short_name": "CC",
            "display": "standalone",
            "background_color": "#0e1116",
            "theme_color": "#0e1116",
            "icons": [
                {"src": "/apple-touch-icon.png", "sizes": "180x180", "type": "image/png"},
                {"src": "/favicon.svg", "sizes": "180x180", "type": "image/svg+xml"},
            ],
        })

    @app.get("/", response_class=HTMLResponse)
    def strategy_view(request: Request):
        view_started = time.perf_counter()
        signals = _read_signals(cfg.db_path, limit=20)
        kill_engaged = Path(cfg.risk.kill_switch_path).exists()
        latest_signal = _read_latest_row(cfg.db_path, "signals")
        latest_order = _read_latest_row(cfg.db_path, "orders")
        latest_equity = _read_latest_row(cfg.db_path, "equity_snapshots")
        latest_signal_age, latest_signal_age_seconds = _age_label(
            latest_signal.get("ts_utc") if latest_signal else None
        )
        latest_order_age, _ = _age_label(latest_order.get("ts_utc") if latest_order else None)
        latest_equity_age, latest_equity_age_seconds = _age_label(
            latest_equity.get("ts_utc") if latest_equity else None
        )

        api_status = "warn"
        api_label = "not checked"
        api_latency_ms = None
        ec = _execution()
        if ec is not None:
            api_started = time.perf_counter()
            try:
                ec.account()
                api_latency_ms = (time.perf_counter() - api_started) * 1000
                api_status = "ok" if api_latency_ms < 1500 else "warn"
                api_label = f"{api_latency_ms:.0f} ms"
            except Exception as e:
                api_status = "bad"
                api_label = str(e)
        else:
            api_status = "bad"
            api_label = "Alpaca client unavailable"

        load_1m = os.getloadavg()[0] if hasattr(os, "getloadavg") else None
        memory_mb = _memory_mb()
        order_status = (latest_order or {}).get("status", "n/a")
        stale_signal = (
            latest_signal_age_seconds is not None
            and latest_signal_age_seconds > cfg.schedule.interval_minutes * 60 * 4
        )
        stale_equity = latest_equity_age_seconds is not None and latest_equity_age_seconds > 3600 * 8
        flags = []
        if kill_engaged:
            flags.append("Kill switch is engaged")
        if api_status == "bad":
            flags.append("Alpaca API unavailable")
        if stale_signal:
            flags.append("Signal history looks stale")
        if stale_equity:
            flags.append("Equity snapshot looks stale")
        if order_status in {"rejected", "canceled"}:
            flags.append(f"Latest order is {order_status}")

        overall_status = "ok" if not flags and api_status == "ok" else ("bad" if api_status == "bad" else "warn")
        selected_strategy = request.query_params.get("strategy", cfg.strategy.name)
        strategy_details = _strategy_details(
            selected_strategy, cfg.strategy.name, cfg.strategy.params
        )
        dashboard_latency_ms = (time.perf_counter() - view_started) * 1000
        return templates.TemplateResponse(request, "strategy.html", {
            "active_tab": "strategy",
            "cfg": cfg,
            "available_strategies": _available_strategies(cfg.strategy.name),
            "strategy_details": strategy_details,
            "signals": signals,
            "latest_signal": latest_signal,
            "latest_order": latest_order,
            "latest_equity": latest_equity,
            "health": {
                "overall_status": overall_status,
                "flags": flags,
                "api_status": api_status,
                "api_label": api_label,
                "dashboard_latency_ms": dashboard_latency_ms,
                "memory_mb": memory_mb,
                "load_1m": load_1m,
                "latest_signal_age": latest_signal_age,
                "latest_order_age": latest_order_age,
                "latest_equity_age": latest_equity_age,
                "order_status": order_status,
            },
            "kill_engaged": kill_engaged,
            "read_only": read_only,
            "mode": "LIVE" if cfg.alpaca.live else "PAPER",
        })

    @app.get("/trades", response_class=HTMLResponse)
    def trades_view(request: Request):
        kill_engaged = Path(cfg.risk.kill_switch_path).exists()
        orders = _read_orders(cfg.db_path, limit=50)
        equity_curve = _read_equity_curve(cfg.db_path)

        ec = _execution()
        if ec is not None:
            try:
                positions = list(ec.positions().values())
                account = ec.account()
                positions = [
                    {"symbol": p.symbol, "qty": p.qty,
                     "market_value": p.market_value, "avg_entry_price": p.avg_entry_price}
                    for p in positions
                ]
                account_data = {"cash": account.cash, "equity": account.equity,
                                "buying_power": account.buying_power}
            except Exception as e:
                positions, account_data = [], {"error": str(e)}
        else:
            positions, account_data = [], {"error": "Alpaca client unavailable"}

        # Compute simple P&L since first equity snapshot.
        pnl = None
        if len(equity_curve) >= 2:
            first = equity_curve[0]["equity"]
            last = equity_curve[-1]["equity"]
            pnl = {
                "abs": last - first,
                "pct": (last - first) / first * 100 if first else 0.0,
                "first_ts": equity_curve[0]["ts_utc"],
                "last_ts": equity_curve[-1]["ts_utc"],
            }

        return templates.TemplateResponse(request, "trades.html", {
            "active_tab": "trades",
            "cfg": cfg,
            "kill_engaged": kill_engaged,
            "positions": positions,
            "account": account_data,
            "orders": orders,
            "equity_curve_json": json.dumps(equity_curve, default=str),
            "pnl": pnl,
            "read_only": read_only,
            "mode": "LIVE" if cfg.alpaca.live else "PAPER",
        })

    @app.get("/risk", response_class=HTMLResponse)
    def risk_view(request: Request):
        kill_path = Path(cfg.risk.kill_switch_path)
        kill_engaged = kill_path.exists()
        kill_reason = kill_path.read_text().strip() if kill_engaged else ""

        # Pull current account + positions to compare against caps.
        ec = _execution()
        account_data = {}
        positions = []
        sod_equity = None
        daily_loss_pct = None

        if ec is not None:
            try:
                acct = ec.account()
                account_data = {
                    "cash": acct.cash, "equity": acct.equity, "buying_power": acct.buying_power
                }
                positions = [
                    {"symbol": p.symbol, "qty": p.qty,
                     "market_value": p.market_value,
                     "pct_of_equity": (p.market_value / acct.equity * 100) if acct.equity else 0.0,
                     "pct_of_cap": (p.market_value / acct.equity / cfg.risk.max_position_pct * 100)
                                   if acct.equity and cfg.risk.max_position_pct else 0.0}
                    for p in ec.positions().values()
                ]
                # Compute daily loss vs the start-of-day equity snapshot.
                today = datetime.now(timezone.utc).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                sod_equity = Storage(cfg.db_path).equity_at_or_before(
                    today.isoformat(timespec="seconds")
                )
                if sod_equity:
                    daily_loss_pct = (sod_equity - acct.equity) / sod_equity * 100
            except Exception as e:
                account_data = {"error": str(e)}

        return templates.TemplateResponse(request, "risk.html", {
            "active_tab": "risk",
            "cfg": cfg,
            "kill_engaged": kill_engaged,
            "kill_reason": kill_reason,
            "kill_path": str(kill_path),
            "account": account_data,
            "positions": positions,
            "sod_equity": sod_equity,
            "daily_loss_pct": daily_loss_pct,
            "read_only": read_only,
            "mode": "LIVE" if cfg.alpaca.live else "PAPER",
        })

    @app.post("/risk/kill-switch/engage")
    def kill_switch_engage():
        if read_only:
            return JSONResponse({"error": "dashboard is read-only"}, status_code=403)
        kill_path = Path(cfg.risk.kill_switch_path)
        kill_path.parent.mkdir(parents=True, exist_ok=True)
        kill_path.write_text(
            f"engaged via dashboard at {datetime.now(timezone.utc).isoformat(timespec='seconds')}"
        )
        return RedirectResponse(url="/risk", status_code=303)

    @app.post("/risk/kill-switch/release")
    def kill_switch_release():
        if read_only:
            return JSONResponse({"error": "dashboard is read-only"}, status_code=403)
        kill_path = Path(cfg.risk.kill_switch_path)
        if kill_path.exists():
            kill_path.unlink()
        return RedirectResponse(url="/risk", status_code=303)

    @app.get("/backtests", response_class=HTMLResponse)
    def backtests_view(request: Request):
        kill_engaged = Path(cfg.risk.kill_switch_path).exists()
        bt_dir = cfg.data_dir / "backtests"
        bt_dir.mkdir(parents=True, exist_ok=True)
        reports = sorted(
            (
                {"name": p.name, "size_kb": p.stat().st_size // 1024,
                 "mtime": p.stat().st_mtime}
                for p in bt_dir.glob("*.html")
            ),
            key=lambda r: r["mtime"], reverse=True,
        )
        return templates.TemplateResponse(request, "backtests.html", {
            "active_tab": "backtests",
            "cfg": cfg,
            "kill_engaged": kill_engaged,
            "reports": list(reports),
            "available_strategies": _available_strategies(cfg.strategy.name),
            "read_only": read_only,
            "mode": "LIVE" if cfg.alpaca.live else "PAPER",
        })

    @app.get("/backtests/{name}")
    def backtest_file(name: str):
        # Whitelist: only files directly under data/backtests with .html extension.
        if "/" in name or "\\" in name or not name.endswith(".html"):
            return JSONResponse({"error": "invalid name"}, status_code=400)
        path = cfg.data_dir / "backtests" / name
        if not path.exists() or not path.is_file():
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(path, media_type="text/html")

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/advisors", response_class=HTMLResponse)
    def advisors_view(request: Request, msg: str = "", err: str = ""):
        kill_engaged = Path(cfg.risk.kill_switch_path).exists()
        return templates.TemplateResponse(request, "advisors.html", {
            "active_tab": "advisors",
            "cfg": cfg,
            "advisor": _advisor_state(cfg),
            "kill_engaged": kill_engaged,
            "read_only": read_only,
            "mode": "LIVE" if cfg.alpaca.live else "PAPER",
            "flash_msg": msg,
            "flash_err": err,
        })

    @app.post("/advisors/refresh")
    def advisors_refresh(force: int = 0):
        """Run all advisors against the current universe and write the result
        to data/advisors/ai_hedge_fund.json (read by /advisors).

        Cached results are reused unless force=1 is set.
        """
        if not cfg.deepseek.enabled:
            return RedirectResponse("/advisors?err=DEEPSEEK_API_KEY+not+configured", 303)

        try:
            data_client = DataClient(cfg.alpaca)
            bars = data_client.daily_bars(cfg.universe, lookback_days=400)
        except Exception as e:
            return RedirectResponse(f"/advisors?err=Alpaca+data+error:+{e}", 303)

        # Build per-symbol context (numeric only — never includes API keys).
        contexts = {}
        for sym in cfg.universe:
            df = bars.get(sym)
            if df is None or df.empty:
                continue
            contexts[sym] = build_context(sym, df)
        if not contexts:
            return RedirectResponse("/advisors?err=No+market+data+returned", 303)

        llm = DeepSeekClient(
            api_key=cfg.deepseek.api_key,
            base_url=cfg.deepseek.base_url,
            default_model=cfg.deepseek.default_model,
        )
        cache = RecommendationCache(cfg.data_dir / "advisors" / "cache.json")
        if force:
            cache.invalidate()

        advisors = build_default_advisors()
        per_symbol_recs: dict[str, list[dict]] = {}
        api_calls = 0
        cache_hits = 0
        for sym, ctx in contexts.items():
            symbol_recs = []
            for advisor in advisors:
                cached = cache.get(advisor.name, sym)
                if cached:
                    symbol_recs.append(cached.to_dict())
                    cache_hits += 1
                    continue
                rec = advisor.recommend(ctx, llm)
                cache.put(rec)
                api_calls += 1
                symbol_recs.append(rec.to_dict())
            per_symbol_recs[sym] = symbol_recs

        # Aggregate "consensus" decision per symbol — backward compat with the
        # existing JSON shape that the dashboard already reads.
        from collections import Counter
        decisions: dict[str, dict] = {}
        for sym, recs in per_symbol_recs.items():
            valid = [r for r in recs if r.get("action") in {"BUY", "HOLD", "SELL"}]
            if not valid:
                decisions[sym] = {"action": "NO DATA", "confidence": None,
                                  "reasoning": "All advisors errored — see per-advisor cells."}
                continue
            counts = Counter(r["action"] for r in valid)
            winner, votes = counts.most_common(1)[0]
            avg_conf = sum(r["confidence"] for r in valid if r["action"] == winner) / max(1, votes)
            decisions[sym] = {
                "action": winner,
                "confidence": round(avg_conf, 2),
                "reasoning": f"{votes}/{len(valid)} advisors recommend {winner} (consensus)",
            }

        out_path = Path(
            os.environ.get(
                "AI_HEDGE_FUND_ADVICE_PATH",
                str(cfg.data_dir / "advisors" / "ai_hedge_fund.json"),
            )
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "service": "AI Hedge Fund (local LLM advisors)",
            "decisions": decisions,
            "advisor_recommendations": per_symbol_recs,
        }, indent=2))
        return RedirectResponse(
            f"/advisors?msg={api_calls}+LLM+calls,+{cache_hits}+cached", 303
        )

    # ---------- Market Summary (7 canned prompts) ----------

    @app.get("/summary", response_class=HTMLResponse)
    def summary_view(request: Request):
        kill_engaged = Path(cfg.risk.kill_switch_path).exists()
        return templates.TemplateResponse(request, "summary.html", {
            "active_tab": "summary",
            "cfg": cfg,
            "kill_engaged": kill_engaged,
            "mode": "LIVE" if cfg.alpaca.live else "PAPER",
            "prompts": PROMPTS,
            "deepseek_configured": cfg.deepseek.enabled,
            "result": None,
            "selected_key": None,
            "user_input": "",
            "error": "",
        })

    @app.post("/summary", response_class=HTMLResponse)
    async def summary_run(request: Request):
        kill_engaged = Path(cfg.risk.kill_switch_path).exists()
        body = (await request.body()).decode()
        form = parse_qs(body)
        key = form.get("prompt_key", [""])[0]
        user_input = form.get("user_input", [""])[0]

        common_ctx = {
            "active_tab": "summary",
            "cfg": cfg,
            "kill_engaged": kill_engaged,
            "mode": "LIVE" if cfg.alpaca.live else "PAPER",
            "prompts": PROMPTS,
            "deepseek_configured": cfg.deepseek.enabled,
            "selected_key": key,
            "user_input": user_input,
        }

        if not cfg.deepseek.enabled:
            return templates.TemplateResponse(request, "summary.html", {
                **common_ctx, "result": None,
                "error": "DEEPSEEK_API_KEY is not configured in /opt/trader/.env",
            })

        prompt = PROMPTS_BY_KEY.get(key)
        if prompt is None:
            return templates.TemplateResponse(request, "summary.html", {
                **common_ctx, "result": None,
                "error": f"Unknown prompt key: {key}",
            })

        # Optionally fetch market data for grounding.
        contexts = []
        if prompt.needs_market_data:
            symbols: list[str]
            if prompt.placeholder_kind == "tickers" and user_input.strip():
                symbols = [s.strip().upper() for s in user_input.replace(",", " ").split() if s.strip()]
            elif prompt.placeholder_kind == "tickers":
                symbols = [s.strip().upper() for s in prompt.placeholder_default.replace(",", " ").split() if s.strip()]
            else:
                symbols = list(cfg.universe)
            try:
                data_client = DataClient(cfg.alpaca)
                bars = data_client.daily_bars(symbols, lookback_days=300)
                contexts = [build_context(sym, df) for sym, df in bars.items() if not df.empty]
            except Exception:
                # Continue without grounding — LLM will note the limitation.
                contexts = []

        llm = DeepSeekClient(
            api_key=cfg.deepseek.api_key,
            base_url=cfg.deepseek.base_url,
            default_model=cfg.deepseek.default_model,
        )
        try:
            result = run_summary(prompt, user_input, llm, contexts)
        except LLMError as e:
            return templates.TemplateResponse(request, "summary.html", {
                **common_ctx, "result": None,
                "error": f"LLM call failed: {e}",
            })

        return templates.TemplateResponse(request, "summary.html", {
            **common_ctx, "result": result, "error": "",
        })

    return app
