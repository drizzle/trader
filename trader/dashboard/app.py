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
import html as html_lib
import inspect
import json
import resource
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import yaml

# All storage + order execution happens in UTC. The dashboard *displays* in
# Pacific Time so it matches the user's wall clock. ZoneInfo automatically
# handles PDT/PST transitions — same instant, correct local label.
_PACIFIC_TZ = ZoneInfo("America/Los_Angeles")


def _to_pacific(value, fmt: str = "%Y-%m-%d %H:%M:%S %Z") -> str:
    """Jinja filter: convert a UTC ISO string or datetime to Pacific Time.

    Accepts:
      - "2026-05-03T18:24:12+00:00" (ISO with offset)
      - "2026-05-03T18:24:12Z"      (ISO with Z)
      - "2026-05-03 18:24:12"       (naive — assumed UTC)
      - datetime objects (naive assumed UTC)

    Falsy / unparseable input returns "" so templates degrade gracefully.
    Default format includes the tz abbreviation so the user sees PDT/PST
    explicitly. Storage and order timestamps stay UTC.
    """
    if not value:
        return ""
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00").replace(" ", "T"))
        except ValueError:
            return value  # not parseable — return as-is
    elif isinstance(value, datetime):
        dt = value
    else:
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_PACIFIC_TZ).strftime(fmt)

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
    # Clean 180x180 PNG with 'CC' rendered on dark background, accent blue.
    # Generated programmatically from a 5x7 bitmap font — fully self-contained.
    "iVBORw0KGgoAAAANSUhEUgAAALQAAAC0CAYAAAA9zQYyAAACnElEQVR42u3dwQnDMBQFQVUQyCW1pcK06eAiLKTVGLYAvT/HQMaY/L3en0vnNAqfQ2p77A6kBG7HUAK28ZWBbXAlUBtZGdiGVQa1QZVBbUhlUBtQGdSGUwq10ZQBbTBlUBtKKdRGUga0gZRCbRxlQBtGKdRGEdDSiqANohRqYwhoCWgJaAloAS3tD9oQSqE2goCWgJaAloAW0BLQEtAS0BLQAloCWgL67vu7tmiVY9kLaKCBdiCggXYgewHtQPYCGmh7AQ000A4ENNAOZC+gHcheQANtL6CBBtqBgAbagey1M+jdocx+n72eex/QQAPtQEAD7UD2AhpoewENNNBAAw20AwENtAPZC2iggQYaaKCBBhpoBwIaaKDtBXQle/n5KNBAAw000A4ENNAOZC+ggbYX0EALaKCBdiCggXYgewENtL2ABlpAAw20AwENtAPZC2ig7QU00AIaaKAdCGigHcheQANtL6CBFtBAA+1AQAPtQPYCGmh7AQ20gAYaaAeyF9AOZC+ggbYX0EAD7UBAA+1A9gLagewFNNBAAw000A4ENNAOZC+ggbYX0EADCzTQQDvQ0++zl79GBhpooIEGGmiggXYgoIEG2l5AAw000EADDTTQQDsQ0EADbS+ggQYaaKCBBhroE0HPfvjsdody0l5AAw20A9kLaAeyF9AOZC+ggQbagYAG2oHsBbQD2QtooO0FNNBAOxDQQDuQvYB2IHv1QEtAS0ALaAloCWgJaAloAS0BLQEtAS0BrTDo+zOEMpiBFtAS0BLQEtAC2iDqgIZaKcxAC2hpZdBQK4UZaOVAQ60UZqCVAw21UpihVg4z0MqBhlopzFArhxlq5TBDrRxmqJXDDLZykKFWEjPYykEGW0nIcCuJGHatgPYPtpsHmQE8ybgAAAAASUVORK5CYII="
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


def _read_allocation_signals(db_path: Path, limit: int = 5000) -> list[dict]:
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT ts_utc, strategy, symbol, target_pct "
            "FROM signals ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


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


def _config_path() -> Path:
    return Path(os.environ.get("CONFIG_PATH", "config.yaml")).resolve()


def _load_config_yaml() -> dict:
    path = _config_path()
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except Exception as e:
        raise RuntimeError(f"Could not read {path}: {e}") from e
    if not isinstance(raw, dict):
        raise RuntimeError(f"{path} is not a YAML mapping")
    return raw


def _write_config_yaml(raw: dict, reason: str) -> None:
    path = _config_path()
    header = (
        "# Strategy + runtime config. Secrets live in .env, NOT here.\n"
        f"# Last updated by dashboard {reason} at "
        f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}\n\n"
    )
    body = yaml.safe_dump(raw, sort_keys=False, default_flow_style=False)
    try:
        path.write_text(header + body)
    except Exception as e:
        raise RuntimeError(f"Could not write {path}: {e}") from e


def _restart_trader_service() -> tuple[bool, str]:
    systemctl = os.environ.get("SYSTEMCTL_BIN", "/bin/systemctl")
    try:
        subprocess.run(
            ["sudo", "-n", systemctl, "restart", "trader"],
            check=True, capture_output=True, text=True, timeout=30,
        )
        check = subprocess.run(
            ["sudo", "-n", systemctl, "is-active", "trader"],
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or e.stdout or str(e)).strip()
        return False, detail or "systemctl restart failed"
    except subprocess.TimeoutExpired:
        return False, "systemctl restart timed out"

    state = (check.stdout or "").strip() or "unknown"
    if state != "active":
        detail = (check.stderr or "").strip()
        return False, f"trader service is {state}; {detail}".strip()
    return True, "trader restarted"


def _apply_risk_update(raw: dict, risk_values: dict, kill_switch_path: str) -> dict:
    updated = dict(raw)
    risk = dict(updated.get("risk") or {})
    risk.update({
        "max_position_pct": risk_values["max_position_pct"],
        "daily_loss_limit_pct": risk_values["daily_loss_limit_pct"],
        "min_cash_buffer_pct": risk_values["min_cash_buffer_pct"],
        "kill_switch_path": kill_switch_path,
    })
    updated["risk"] = risk
    return updated


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


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
    read_only = _env_bool("DASHBOARD_READ_ONLY", True)
    broker_reads_enabled = _env_bool("DASHBOARD_ENABLE_BROKER_READS", False)
    require_auth = _env_bool(
        "DASHBOARD_REQUIRE_AUTH",
        bool(dashboard_password) or not read_only or cfg.alpaca.live,
    )
    if require_auth and not dashboard_password:
        raise RuntimeError(
            "DASHBOARD_PASSWORD must be set when auth is required "
            "(write mode and live mode fail closed)."
        )
    session_seconds = _env_int("DASHBOARD_SESSION_SECONDS", 300)
    sessions: dict[str, dict] = {}
    login_failures: dict[str, list[float]] = {}
    action_hits: dict[str, list[float]] = {}

    def _client_key(request: Request) -> str:
        forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        return forwarded or (request.client.host if request.client else "unknown")

    def _allow_rate(bucket: dict[str, list[float]], key: str, limit: int, window: int) -> bool:
        now = time.time()
        hits = [t for t in bucket.get(key, []) if now - t < window]
        if len(hits) >= limit:
            bucket[key] = hits
            return False
        hits.append(now)
        bucket[key] = hits
        return True

    def _authenticated(request: Request) -> bool:
        if not require_auth:
            return True
        if not dashboard_password:
            return False
        token = request.cookies.get("trader_session")
        if not token:
            return False
        now = datetime.now(timezone.utc)
        session = sessions.get(token)
        if session is None:
            return False
        last_seen = session.get("last_seen")
        if last_seen is None or now - last_seen > timedelta(seconds=session_seconds):
            sessions.pop(token, None)
            return False
        session["last_seen"] = now
        return True

    def _session(request: Request) -> dict | None:
        token = request.cookies.get("trader_session")
        return sessions.get(token) if token else None

    def _csrf_token(request: Request) -> str:
        session = _session(request)
        if not session:
            return ""
        token = session.get("csrf")
        if not token:
            token = secrets.token_urlsafe(32)
            session["csrf"] = token
        return token

    async def _csrf_valid(request: Request) -> bool:
        if not require_auth:
            return True
        session = _session(request)
        if not session:
            return False
        expected = session.get("csrf")
        if not expected:
            return False
        if secrets.compare_digest(request.headers.get("x-csrf-token") or "", expected):
            return True
        body = (await request.body()).decode()
        form = parse_qs(body)
        supplied = form.get("csrf_token", [""])[0]
        return bool(supplied) and secrets.compare_digest(supplied, expected)

    def _reauth_valid(form: dict[str, list[str]], request: Request) -> bool:
        if not require_auth:
            return True
        password = form.get("action_password", [""])[0]
        if not (dashboard_password and secrets.compare_digest(password, dashboard_password)):
            return False
        session = _session(request)
        if session is not None:
            session["reauth_at"] = datetime.now(timezone.utc)
        return True

    app = FastAPI(title="trader dashboard")

    # Templates live next to this file.
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    # Make `{{ ts | pacific }}` available everywhere. Storage stays in UTC;
    # only the rendered display is converted.
    templates.env.filters["pacific"] = _to_pacific

    def _with_csrf(request: Request, context: dict) -> dict:
        return {**context, "csrf_token": _csrf_token(request)}

    # Track in-flight backtest jobs so the UI can show a "running" indicator.
    _running_backtests: set[str] = set()

    # We instantiate ExecutionClient lazily on each request because a brief
    # Alpaca outage shouldn't bring the dashboard down at startup.
    def _execution() -> ExecutionClient | None:
        if not broker_reads_enabled:
            return None
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
        if request.method == "POST" and path not in {"/login"}:
            if not await _csrf_valid(request):
                return JSONResponse({"error": "invalid CSRF token"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @app.get("/login", response_class=HTMLResponse)
    def login_view():
        return _login_page()

    def _request_is_https(request: Request) -> bool:
        """Did the client connect over HTTPS?

        When Caddy terminates TLS and reverse-proxies to uvicorn over HTTP,
        `request.url.scheme` is "http". Caddy sets `X-Forwarded-Proto: https`
        so we can recover the original scheme. For an SSH-tunnel scenario
        (no proxy at all) the connection is plain HTTP, and we must NOT mark
        the session cookie Secure or the browser will silently drop it,
        producing an infinite login → / → login redirect loop.
        """
        if request.url.scheme == "https":
            return True
        return (request.headers.get("x-forwarded-proto") or "").lower() == "https"

    @app.post("/login")
    async def login_submit(request: Request):
        client = _client_key(request)
        if not _allow_rate(login_failures, client, limit=8, window=300):
            return _login_page("Too many login attempts. Wait a few minutes and try again.")
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
        sessions[token] = {
            "last_seen": datetime.now(timezone.utc),
            "csrf": secrets.token_urlsafe(32),
            "reauth_at": None,
        }
        login_failures.pop(client, None)
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(
            "trader_session",
            token,
            max_age=session_seconds,
            httponly=True,
            secure=_request_is_https(request),
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
        # Simple CC mark — dark rounded square, blue letters. No nested boxes.
        return HTMLResponse(
            """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 180 180">
<rect width="180" height="180" rx="32" fill="#0e1116"/>
<text x="90" y="118" text-anchor="middle" font-family="-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif" font-size="92" font-weight="800" fill="#58a6ff" letter-spacing="-4">CC</text>
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

    @app.get("/")
    def root_redirect():
        return RedirectResponse(url="/trades", status_code=307)

    @app.get("/strategy", response_class=HTMLResponse)
    def strategy_view(request: Request, msg: str = "", err: str = ""):
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
        if not broker_reads_enabled:
            api_status = "warn"
            api_label = "broker reads disabled"
        elif ec is not None:
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
        return templates.TemplateResponse(request, "strategy.html", _with_csrf(request, {
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
            "flash_msg": msg,
            "flash_err": err,
        }))

    @app.get("/strategy/deploy", response_class=HTMLResponse)
    def strategy_deploy_view(request: Request, name: str = "", err: str = ""):
        """Confirmation page for switching the deployed strategy."""
        if read_only:
            return HTMLResponse(
                "Dashboard is read-only. Set DASHBOARD_READ_ONLY=false to enable deploys.",
                status_code=403,
            )
        if not name or name not in STRATEGIES:
            return RedirectResponse(
                url="/strategy?err=Unknown+or+missing+strategy+name", status_code=303
            )
        if name == cfg.strategy.name:
            return RedirectResponse(
                url=f"/strategy?err={name}+is+already+the+active+strategy",
                status_code=303,
            )
        details = _strategy_details(name, cfg.strategy.name, cfg.strategy.params)
        kill_engaged = Path(cfg.risk.kill_switch_path).exists()
        return templates.TemplateResponse(request, "strategy_deploy.html", _with_csrf(request, {
            "active_tab": "strategy",
            "cfg": cfg,
            "details": details,
            "current_name": cfg.strategy.name,
            "current_universe": cfg.universe,
            "current_params": cfg.strategy.params,
            "kill_engaged": kill_engaged,
            "read_only": read_only,
            "broker_reads_enabled": broker_reads_enabled,
            "mode": "LIVE" if cfg.alpaca.live else "PAPER",
            "flash_err": err,
        }))

    @app.post("/strategy/deploy")
    async def strategy_deploy_submit(request: Request):
        """Run `python -m trader switch-strategy ...` as a subprocess.

        Important: the dashboard NEVER calls Alpaca's trading API directly.
        All flatten/order activity happens inside the CLI subprocess, which
        runs as the trader user with .env credentials.
        """
        if read_only:
            return JSONResponse({"error": "dashboard is read-only"}, status_code=403)

        body = (await request.body()).decode()
        form = parse_qs(body)
        name = (form.get("name", [""])[0] or "").strip()
        flatten = form.get("flatten", [""])[0] == "on"
        confirm = (form.get("confirm", [""])[0] or "").strip()
        if not _allow_rate(action_hits, f"{_client_key(request)}:strategy", 5, 300):
            return RedirectResponse(url="/strategy?err=Too+many+strategy+deploy+attempts", status_code=303)

        if name not in STRATEGIES:
            return RedirectResponse(
                url="/strategy?err=Unknown+strategy", status_code=303
            )
        if not _reauth_valid(form, request):
            return RedirectResponse(
                url=f"/strategy/deploy?name={name}&err=Dashboard+password+did+not+match",
                status_code=303,
            )
        if confirm != name:
            return RedirectResponse(
                url=f"/strategy/deploy?name={name}&err=Confirmation+text+did+not+match",
                status_code=303,
            )
        if name == cfg.strategy.name:
            return RedirectResponse(
                url=f"/strategy?err={name}+is+already+active", status_code=303,
            )
        if flatten and not broker_reads_enabled:
            return RedirectResponse(
                url=f"/strategy/deploy?name={name}&err=Flatten+from+dashboard+is+disabled+because+dashboard+does+not+hold+Alpaca+keys",
                status_code=303,
            )

        cmd = [
            sys.executable, "-m", "trader", "switch-strategy",
            "--name", name, "--restart",
        ]
        if flatten:
            cmd.append("--flatten")

        try:
            proc = subprocess.run(
                cmd,
                cwd=str(Path(__file__).resolve().parents[2]),
                capture_output=True, text=True, timeout=120,
            )
        except subprocess.TimeoutExpired:
            return RedirectResponse(
                url="/strategy?err=Deploy+timed+out+after+120s+-+check+journalctl",
                status_code=303,
            )

        if proc.returncode == 0:
            from urllib.parse import quote
            return RedirectResponse(
                url=f"/strategy?msg={quote(f'Deployed {name}. Kill switch is engaged — release it on /risk when ready to trade.')}",
                status_code=303,
            )

        # Surface the last useful line of stderr so the user can see why.
        stderr_lines = [
            ln for ln in (proc.stderr or "").splitlines()
            if ln.strip() and "ERROR" in ln.upper()
        ]
        last = stderr_lines[-1] if stderr_lines else (proc.stderr or proc.stdout or "")[-300:]
        from urllib.parse import quote
        return RedirectResponse(
            url=f"/strategy?err={quote(f'Deploy failed (rc={proc.returncode}): {last}')}",
            status_code=303,
        )

    @app.get("/trades", response_class=HTMLResponse)
    def trades_view(request: Request):
        kill_engaged = Path(cfg.risk.kill_switch_path).exists()
        orders = _read_orders(cfg.db_path, limit=50)
        chart_orders = _read_orders(cfg.db_path, limit=500)
        equity_curve = _read_equity_curve(cfg.db_path)
        allocation_signals = _read_allocation_signals(cfg.db_path)

        ec = _execution()
        if ec is not None:
            try:
                raw_positions = list(ec.positions().values())
                account = ec.account()
                # Per-unit market price = market_value / qty. Alpaca returns
                # market_value as the live mark, so this is the current quote
                # without an extra API call. Both unrealized P&L per unit
                # ($ delta) and % delta are derived from there.
                positions = []
                for p in raw_positions:
                    current_price = (p.market_value / p.qty) if p.qty else 0.0
                    unrealized_per_unit = current_price - p.avg_entry_price
                    cost_basis = p.avg_entry_price * p.qty
                    unrealized_total = p.market_value - cost_basis
                    unrealized_pct = (
                        (unrealized_per_unit / p.avg_entry_price * 100)
                        if p.avg_entry_price else 0.0
                    )
                    positions.append({
                        "symbol": p.symbol,
                        "qty": p.qty,
                        "avg_entry_price": p.avg_entry_price,
                        "current_price": current_price,
                        "market_value": p.market_value,
                        "unrealized_pl": unrealized_total,
                        "unrealized_pl_pct": unrealized_pct,
                    })
                account_data = {"cash": account.cash, "equity": account.equity,
                                "buying_power": account.buying_power}
            except Exception as e:
                positions, account_data = [], {"error": str(e)}
        else:
            msg = "Broker reads disabled for dashboard" if not broker_reads_enabled else "Alpaca client unavailable"
            positions, account_data = [], {"error": msg}

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

        # Allocation label priority — derived from what we *actually traded*,
        # not what the strategy config says today. Order:
        #   1. Currently held positions (most accurate "what's in the holding band")
        #   2. Most recent FILLED order (covers cases where positions just closed)
        #   3. cfg.universe (last-resort fallback)
        # Without this, switching strategy from BTC to SPY would relabel the
        # historical bars as "SPY" even though those bars represent BTC value.
        non_zero_positions = [p for p in positions if p.get("qty", 0)]
        recent_fill_symbol = None
        for o in orders:
            if o.get("status") == "filled" and o.get("symbol"):
                recent_fill_symbol = o["symbol"]
                break

        signal_symbols = []
        seen_signal_symbols = set()
        for s in allocation_signals:
            sym = s.get("symbol")
            if sym and sym not in seen_signal_symbols:
                seen_signal_symbols.add(sym)
                signal_symbols.append(sym)

        if signal_symbols:
            allocation_asset_label = (
                signal_symbols[0] if len(signal_symbols) == 1
                else f"{len(signal_symbols)} assets"
            )
        elif len(non_zero_positions) == 1:
            allocation_asset_label = non_zero_positions[0]["symbol"]
        elif len(non_zero_positions) > 1:
            allocation_asset_label = " + ".join(p["symbol"] for p in non_zero_positions[:3])
            if len(non_zero_positions) > 3:
                allocation_asset_label += f" +{len(non_zero_positions) - 3}"
        elif recent_fill_symbol:
            allocation_asset_label = recent_fill_symbol
        elif cfg.universe:
            allocation_asset_label = cfg.universe[0] if len(cfg.universe) == 1 else "Holdings"
        else:
            allocation_asset_label = "Holdings"

        # Latest equity-snapshot timestamp + page render time, so the user
        # can see how stale the data is and roughly when the next tick lands.
        latest_equity_ts = equity_curve[-1]["ts_utc"] if equity_curve else None
        return templates.TemplateResponse(request, "trades.html", _with_csrf(request, {
            "active_tab": "trades",
            "cfg": cfg,
            "kill_engaged": kill_engaged,
            "positions": positions,
            "account": account_data,
            "orders": orders,
            "equity_curve_json": json.dumps(equity_curve, default=str),
            "chart_orders_json": json.dumps(chart_orders, default=str),
            "allocation_signals_json": json.dumps(allocation_signals, default=str),
            "allocation_asset_label": allocation_asset_label,
            "pnl": pnl,
            "read_only": read_only,
            "mode": "LIVE" if cfg.alpaca.live else "PAPER",
            "page_generated_at": _to_pacific(datetime.now(timezone.utc)),
            "latest_equity_ts": latest_equity_ts,
            "tick_interval_minutes": cfg.schedule.interval_minutes,
        }))

    @app.get("/risk", response_class=HTMLResponse)
    def risk_view(request: Request, msg: str = "", err: str = ""):
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
        else:
            account_data = {
                "error": (
                    "Broker reads disabled for dashboard"
                    if not broker_reads_enabled else "Alpaca client unavailable"
                )
            }

        return templates.TemplateResponse(request, "risk.html", _with_csrf(request, {
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
            "flash_msg": msg,
            "flash_err": err,
        }))

    @app.post("/risk/deploy")
    async def risk_deploy_submit(request: Request):
        if read_only:
            return JSONResponse({"error": "dashboard is read-only"}, status_code=403)

        body = (await request.body()).decode()
        form = parse_qs(body)
        if not _allow_rate(action_hits, f"{_client_key(request)}:risk", 8, 300):
            return RedirectResponse(url="/risk?err=Too+many+risk+change+attempts", status_code=303)
        if not _reauth_valid(form, request):
            return RedirectResponse(url="/risk?err=Dashboard+password+did+not+match", status_code=303)

        def _pct_field(name: str, label: str) -> float:
            raw = (form.get(name, [""])[0] or "").strip()
            try:
                value = float(raw)
            except ValueError as e:
                raise ValueError(f"{label} must be a number") from e
            if value < 0 or value > 100:
                raise ValueError(f"{label} must be between 0 and 100")
            return value / 100.0

        try:
            max_position_pct = _pct_field("max_position_pct", "Max position")
            daily_loss_limit_pct = _pct_field("daily_loss_limit_pct", "Daily loss cap")
            min_cash_buffer_pct = _pct_field("min_cash_buffer_pct", "Min cash buffer")
            if max_position_pct + min_cash_buffer_pct > 1.0:
                raise ValueError("Max position plus min cash buffer cannot exceed 100%")

            raw = _apply_risk_update(_load_config_yaml(), {
                "max_position_pct": max_position_pct,
                "daily_loss_limit_pct": daily_loss_limit_pct,
                "min_cash_buffer_pct": min_cash_buffer_pct,
            }, cfg.risk.kill_switch_path)
            _write_config_yaml(raw, "risk update")
        except Exception as e:
            from urllib.parse import quote
            return RedirectResponse(url=f"/risk?err={quote(str(e))}", status_code=303)

        # Keep the currently running dashboard view in sync. The trader process
        # still needs a restart to load the file on its next boot.
        cfg.risk.max_position_pct = max_position_pct
        cfg.risk.daily_loss_limit_pct = daily_loss_limit_pct
        cfg.risk.min_cash_buffer_pct = min_cash_buffer_pct

        ok, detail = _restart_trader_service()
        from urllib.parse import quote
        if not ok:
            return RedirectResponse(
                url=f"/risk?err={quote('Risk config saved, but trader restart failed: ' + detail)}",
                status_code=303,
            )
        return RedirectResponse(
            url="/risk?msg=Risk+config+saved+and+trader+restarted",
            status_code=303,
        )

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
    async def kill_switch_release(request: Request):
        if read_only:
            return JSONResponse({"error": "dashboard is read-only"}, status_code=403)
        body = (await request.body()).decode()
        form = parse_qs(body)
        if not _allow_rate(action_hits, f"{_client_key(request)}:kill-release", 5, 300):
            return RedirectResponse(url="/risk?err=Too+many+kill-switch+release+attempts", status_code=303)
        if not _reauth_valid(form, request):
            return RedirectResponse(url="/risk?err=Dashboard+password+did+not+match", status_code=303)
        kill_path = Path(cfg.risk.kill_switch_path)
        if kill_path.exists():
            kill_path.unlink()
        return RedirectResponse(url="/risk", status_code=303)

    @app.get("/backtests", response_class=HTMLResponse)
    def backtests_view(request: Request, msg: str = "", err: str = ""):
        kill_engaged = Path(cfg.risk.kill_switch_path).exists()
        bt_dir = cfg.data_dir / "backtests"
        bt_dir.mkdir(parents=True, exist_ok=True)

        def _fmt_mtime(ts: float) -> str:
            try:
                return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            except Exception:
                return str(int(ts))

        reports = sorted(
            (
                {"name": p.name, "size_kb": p.stat().st_size // 1024,
                 "mtime": p.stat().st_mtime,
                 "mtime_label": _fmt_mtime(p.stat().st_mtime)}
                for p in bt_dir.glob("*.html")
            ),
            key=lambda r: r["mtime"], reverse=True,
        )
        return templates.TemplateResponse(request, "backtests.html", _with_csrf(request, {
            "active_tab": "backtests",
            "cfg": cfg,
            "kill_engaged": kill_engaged,
            "reports": list(reports),
            "available_strategies": _available_strategies(cfg.strategy.name),
            "running_jobs": list(_running_backtests),
            "read_only": read_only,
            "broker_reads_enabled": broker_reads_enabled,
            "mode": "LIVE" if cfg.alpaca.live else "PAPER",
            "flash_msg": msg,
            "flash_err": err,
        }))

    @app.post("/backtests/run")
    async def backtests_run(request: Request):
        if read_only:
            return JSONResponse({"error": "dashboard is read-only"}, status_code=403)
        if not broker_reads_enabled:
            return RedirectResponse(
                url="/backtests?err=Broker+data+reads+are+disabled+for+the+dashboard",
                status_code=303,
            )
        if not _allow_rate(action_hits, f"{_client_key(request)}:backtests", 3, 900):
            return RedirectResponse(url="/backtests?err=Too+many+backtest+launches", status_code=303)
        if len(_running_backtests) >= 1:
            return RedirectResponse(url="/backtests?err=A+backtest+is+already+running", status_code=303)
        body = (await request.body()).decode()
        form = parse_qs(body)
        strategy = (form.get("strategy", [""])[0] or "").strip()
        years_raw = (form.get("years", [""])[0] or "").strip()
        valid_strategies = {s["name"] for s in _available_strategies(cfg.strategy.name)}
        if strategy not in valid_strategies:
            return RedirectResponse(url="/backtests?err=Unknown+strategy", status_code=303)
        try:
            years = int(years_raw)
        except ValueError:
            return RedirectResponse(url="/backtests?err=Invalid+lookback", status_code=303)
        if years not in (1, 2, 5, 10):
            return RedirectResponse(url="/backtests?err=Lookback+must+be+1%2C+2%2C+5+or+10+years", status_code=303)

        end = datetime.now(timezone.utc).date()
        start = end.replace(year=end.year - years)
        job_id = f"{strategy}-{years}y-{int(time.time())}"
        cmd = [
            sys.executable, "-m", "trader", "backtest",
            "--strategy", strategy,
            "--start", start.isoformat(),
            "--end", end.isoformat(),
        ]

        def _run():
            _running_backtests.add(job_id)
            try:
                subprocess.run(cmd, cwd=str(Path(__file__).resolve().parents[2]),
                               check=False, capture_output=True, timeout=15 * 60)
            except Exception:
                pass
            finally:
                _running_backtests.discard(job_id)

        threading.Thread(target=_run, daemon=True).start()
        return RedirectResponse(
            url=f"/backtests?msg=Launched+{strategy}+%C2%B7+{years}y+%E2%80%94+report+will+appear+below+when+complete",
            status_code=303,
        )

    @app.get("/backtests/{name}")
    def backtest_file(name: str, raw: int = 0):
        # Whitelist: only files directly under data/backtests with .html extension.
        if "/" in name or "\\" in name or not name.endswith(".html"):
            return JSONResponse({"error": "invalid name"}, status_code=400)
        path = cfg.data_dir / "backtests" / name
        if not path.exists() or not path.is_file():
            return JSONResponse({"error": "not found"}, status_code=404)
        report_headers = {
            "Content-Security-Policy": (
                "sandbox allow-top-navigation-by-user-activation; "
                "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:"
            ),
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        }
        # raw=1 serves the report directly (no back bar — used by "Open raw" link).
        if raw:
            return FileResponse(path, media_type="text/html", headers=report_headers)
        # Default: inject a sticky back-bar directly into the report's <body>.
        # This avoids iframe rendering issues (CSP, viewport, white-on-white)
        # while still letting the user return to the dashboard from any report.
        try:
            html = path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return JSONResponse({"error": f"could not read report: {e}"}, status_code=500)

        bar = (
            '<div id="__trader_back_bar" style="'
            'position:sticky;top:0;z-index:99999;'
            'display:flex;align-items:center;gap:14px;'
            'padding:10px 16px;background:#0f1c17;'
            'border-bottom:1px solid #1a2a22;'
            'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',system-ui,sans-serif;'
            '">'
            '<a href="/backtests" style="'
            'color:#4ade80;text-decoration:none;font-weight:600;font-size:14px;'
            'display:inline-flex;align-items:center;gap:6px;'
            'padding:8px 14px;border-radius:6px;min-height:40px;'
            'border:1px solid rgba(74,222,128,0.35);'
            'background:rgba(74,222,128,0.10);'
            '">← Back to Dashboard</a>'
            f'<span style="color:#b8c7be;font-family:ui-monospace,\'JetBrains Mono\',monospace;'
            f'font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1">{html_lib.escape(name)}</span>'
            f'<a href="/backtests/{html_lib.escape(name)}?raw=1" target="_blank" rel="noopener" style="'
            'color:#6f8479;text-decoration:none;font-size:13px;padding:6px 10px">Open raw ↗</a>'
            '</div>'
        )

        # Insert the bar right after the opening <body...> tag if present;
        # otherwise prepend (so the link is always visible even on malformed reports).
        lower = html.lower()
        idx = lower.find("<body")
        if idx != -1:
            close = html.find(">", idx)
            if close != -1:
                html = html[: close + 1] + bar + html[close + 1 :]
            else:
                html = bar + html
        else:
            html = bar + html
        return HTMLResponse(html, headers=report_headers)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/advisors", response_class=HTMLResponse)
    def advisors_view(request: Request, msg: str = "", err: str = ""):
        kill_engaged = Path(cfg.risk.kill_switch_path).exists()
        return templates.TemplateResponse(request, "advisors.html", _with_csrf(request, {
            "active_tab": "advisors",
            "cfg": cfg,
            "advisor": _advisor_state(cfg),
            "kill_engaged": kill_engaged,
            "read_only": read_only,
            "mode": "LIVE" if cfg.alpaca.live else "PAPER",
            "flash_msg": msg,
            "flash_err": err,
        }))

    @app.post("/advisors/refresh")
    def advisors_refresh(request: Request, force: int = 0):
        """Run all advisors against the current universe and write the result
        to data/advisors/ai_hedge_fund.json (read by /advisors).

        Cached results are reused unless force=1 is set.
        """
        if read_only:
            return JSONResponse({"error": "dashboard is read-only"}, status_code=403)
        if not _allow_rate(action_hits, f"{_client_key(request)}:advisors", 3, 900):
            return RedirectResponse("/advisors?err=Too+many+advisor+refreshes", 303)
        if not broker_reads_enabled:
            return RedirectResponse("/advisors?err=Broker+data+reads+are+disabled+for+the+dashboard", 303)
        if not cfg.deepseek.enabled:
            return RedirectResponse("/advisors?err=DEEPSEEK_API_KEY+not+configured", 303)

        try:
            data_client = DataClient(cfg.alpaca)
            # Use the mixed-universe fetcher so BTC/USD and similar crypto
            # symbols hit the crypto endpoint instead of returning empty.
            bars = data_client.bars_for_universe(cfg.universe, lookback_days=400)
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
        return templates.TemplateResponse(request, "summary.html", _with_csrf(request, {
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
        }))

    @app.post("/summary", response_class=HTMLResponse)
    async def summary_run(request: Request):
        kill_engaged = Path(cfg.risk.kill_switch_path).exists()
        if read_only:
            return JSONResponse({"error": "dashboard is read-only"}, status_code=403)
        if not _allow_rate(action_hits, f"{_client_key(request)}:summary", 8, 900):
            return JSONResponse({"error": "too many summary requests"}, status_code=429)
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
            return templates.TemplateResponse(request, "summary.html", _with_csrf(request, {
                **common_ctx, "result": None,
                "error": "DEEPSEEK_API_KEY is not configured in /opt/trader/.env",
            }))

        prompt = PROMPTS_BY_KEY.get(key)
        if prompt is None:
            return templates.TemplateResponse(request, "summary.html", _with_csrf(request, {
                **common_ctx, "result": None,
                "error": f"Unknown prompt key: {key}",
            }))

        # Optionally fetch market data for grounding.
        contexts = []
        if prompt.needs_market_data:
            if not broker_reads_enabled:
                contexts = []
                symbols = []
            else:
                symbols: list[str]
                if prompt.placeholder_kind == "tickers" and user_input.strip():
                    symbols = [s.strip().upper() for s in user_input.replace(",", " ").split() if s.strip()]
                elif prompt.placeholder_kind == "tickers":
                    symbols = [s.strip().upper() for s in prompt.placeholder_default.replace(",", " ").split() if s.strip()]
                else:
                    symbols = list(cfg.universe)
                try:
                    data_client = DataClient(cfg.alpaca)
                    # Mixed-universe fetcher routes BTC/USD-style symbols through
                    # the crypto endpoint; equity tickers stay on IEX.
                    bars = data_client.bars_for_universe(symbols, lookback_days=300)
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
            return templates.TemplateResponse(request, "summary.html", _with_csrf(request, {
                **common_ctx, "result": None,
                "error": f"LLM call failed: {e}",
            }))

        return templates.TemplateResponse(request, "summary.html", _with_csrf(request, {
            **common_ctx, "result": result, "error": "",
        }))

    return app
