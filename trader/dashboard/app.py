"""FastAPI dashboard.

Four views:
  /            → strategy summary (latest signals, current target, kill-switch state)
  /trades      → positions + recent fills + equity curve since deployment
  /risk        → all risk caps, current exposures vs limits, kill-switch ops
  /backtests   → list of HTML reports under data/backtests/

Read-only (mostly — /risk has a kill-switch toggle). Binds to 127.0.0.1 by
default — access via SSH tunnel:
    ssh -L 8000:localhost:8000 root@<droplet-ip>
    open http://localhost:8000
"""
from __future__ import annotations

import os
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..config import Config
from ..execution import ExecutionClient
from ..storage import Storage
from ..strategy import STRATEGIES


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
    <input id="username" name="username" autocomplete="off" autocapitalize="none" required>
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
        if path not in {"/login", "/logout", "/healthz"} and not _authenticated(request):
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

    @app.get("/", response_class=HTMLResponse)
    def strategy_view(request: Request):
        signals = _read_signals(cfg.db_path, limit=20)
        kill_engaged = Path(cfg.risk.kill_switch_path).exists()
        return templates.TemplateResponse(request, "strategy.html", {
            "active_tab": "strategy",
            "cfg": cfg,
            "available_strategies": _available_strategies(cfg.strategy.name),
            "signals": signals,
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

    return app
