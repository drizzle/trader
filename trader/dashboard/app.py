"""FastAPI dashboard.

Three views:
  /            → strategy summary (latest signals, current target, kill-switch state)
  /trades      → positions + recent fills + equity curve since deployment
  /backtests   → list of HTML reports under data/backtests/

Read-only. Binds to 127.0.0.1 by default — access via SSH tunnel:
    ssh -L 8000:localhost:8000 root@<droplet-ip>
    open http://localhost:8000
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from ..config import Config
from ..execution import ExecutionClient
from ..storage import Storage


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


def create_app(cfg: Config) -> FastAPI:
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

    @app.get("/", response_class=HTMLResponse)
    def strategy_view(request: Request):
        signals = _read_signals(cfg.db_path, limit=20)
        kill_engaged = Path(cfg.risk.kill_switch_path).exists()
        return templates.TemplateResponse("strategy.html", {
            "request": request,
            "active_tab": "strategy",
            "cfg": cfg,
            "signals": signals,
            "kill_engaged": kill_engaged,
            "mode": "LIVE" if cfg.alpaca.live else "PAPER",
        })

    @app.get("/trades", response_class=HTMLResponse)
    def trades_view(request: Request):
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

        return templates.TemplateResponse("trades.html", {
            "request": request,
            "active_tab": "trades",
            "cfg": cfg,
            "positions": positions,
            "account": account_data,
            "orders": orders,
            "equity_curve_json": json.dumps(equity_curve, default=str),
            "pnl": pnl,
            "mode": "LIVE" if cfg.alpaca.live else "PAPER",
        })

    @app.get("/backtests", response_class=HTMLResponse)
    def backtests_view(request: Request):
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
        return templates.TemplateResponse("backtests.html", {
            "request": request,
            "active_tab": "backtests",
            "cfg": cfg,
            "reports": list(reports),
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
