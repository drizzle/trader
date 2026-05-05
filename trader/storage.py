"""SQLite storage for signals, orders, fills, and end-of-day equity snapshots."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc      TEXT NOT NULL,
    strategy    TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    target_pct  REAL NOT NULL,
    rationale   TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc          TEXT NOT NULL,
    client_order_id TEXT UNIQUE NOT NULL,
    broker_order_id TEXT,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,           -- 'buy' or 'sell'
    qty             REAL NOT NULL,
    order_type      TEXT NOT NULL,           -- 'market', 'limit'
    limit_price     REAL,
    status          TEXT NOT NULL,           -- 'submitted', 'filled', 'partial', 'rejected', 'canceled'
    filled_qty      REAL DEFAULT 0,
    filled_avg_price REAL,
    strategy        TEXT,
    error           TEXT
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc      TEXT NOT NULL,
    cash        REAL NOT NULL,
    equity      REAL NOT NULL,
    buying_power REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS position_snapshots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_ts_utc  TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    qty              REAL NOT NULL,
    market_value     REAL NOT NULL,
    avg_entry_price  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS position_snapshot_meta (
    snapshot_ts_utc TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS pending_actions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc       TEXT NOT NULL,
    kind         TEXT NOT NULL,
    status       TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    error        TEXT
);

CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts_utc);
CREATE INDEX IF NOT EXISTS idx_orders_ts  ON orders(ts_utc);
CREATE INDEX IF NOT EXISTS idx_equity_ts  ON equity_snapshots(ts_utc);
CREATE INDEX IF NOT EXISTS idx_positions_snapshot_ts ON position_snapshots(snapshot_ts_utc);
CREATE INDEX IF NOT EXISTS idx_pending_actions_status ON pending_actions(kind, status, id);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class Storage:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, isolation_level=None)  # autocommit
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    # --- writes ---

    def record_signal(
        self, strategy: str, symbol: str, target_pct: float, rationale: str = ""
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO signals (ts_utc, strategy, symbol, target_pct, rationale) "
                "VALUES (?, ?, ?, ?, ?)",
                (_utcnow(), strategy, symbol, target_pct, rationale),
            )
            return cur.lastrowid

    def record_order_submitted(
        self,
        client_order_id: str,
        broker_order_id: str | None,
        symbol: str,
        side: str,
        qty: float,
        order_type: str,
        limit_price: float | None,
        strategy: str,
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO orders (ts_utc, client_order_id, broker_order_id, symbol, side, "
                "qty, order_type, limit_price, status, strategy) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'submitted', ?)",
                (_utcnow(), client_order_id, broker_order_id, symbol, side, qty,
                 order_type, limit_price, strategy),
            )
            return cur.lastrowid

    def record_order_failed(
        self, client_order_id: str, symbol: str, side: str, qty: float, error: str, strategy: str
    ) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO orders (ts_utc, client_order_id, symbol, side, qty, "
                "order_type, status, error, strategy) "
                "VALUES (?, ?, ?, ?, ?, 'market', 'rejected', ?, ?)",
                (_utcnow(), client_order_id, symbol, side, qty, error, strategy),
            )

    def update_order_fill(
        self, client_order_id: str, status: str, filled_qty: float, filled_avg_price: float | None
    ) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE orders SET status = ?, filled_qty = ?, filled_avg_price = ? "
                "WHERE client_order_id = ?",
                (status, filled_qty, filled_avg_price, client_order_id),
            )

    def record_equity(self, cash: float, equity: float, buying_power: float) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO equity_snapshots (ts_utc, cash, equity, buying_power) "
                "VALUES (?, ?, ?, ?)",
                (_utcnow(), cash, equity, buying_power),
            )

    def record_position_snapshot(self, positions: list[dict[str, Any]]) -> str:
        ts = _utcnow()
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO position_snapshot_meta (snapshot_ts_utc) VALUES (?)",
                (ts,),
            )
            for p in positions:
                c.execute(
                    "INSERT INTO position_snapshots "
                    "(snapshot_ts_utc, symbol, qty, market_value, avg_entry_price) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        ts,
                        str(p["symbol"]),
                        float(p.get("qty", 0.0)),
                        float(p.get("market_value", 0.0)),
                        float(p.get("avg_entry_price", 0.0)),
                    ),
                )
        return ts

    def stage_strategy_switch(
        self,
        name: str,
        *,
        flatten: bool,
        restart: bool,
        reason: str,
    ) -> int:
        payload = {
            "name": name,
            "flatten": bool(flatten),
            "restart": bool(restart),
            "reason": reason,
        }
        with self._conn() as c:
            c.execute(
                "UPDATE pending_actions SET status = 'canceled' "
                "WHERE kind = 'strategy_switch' AND status IN ('pending', 'processing')"
            )
            cur = c.execute(
                "INSERT INTO pending_actions (ts_utc, kind, status, payload_json) "
                "VALUES (?, 'strategy_switch', 'pending', ?)",
                (_utcnow(), json.dumps(payload, sort_keys=True)),
            )
            return int(cur.lastrowid)

    def mark_pending_action(self, action_id: int, status: str, error: str | None = None) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE pending_actions SET status = ?, error = ? WHERE id = ?",
                (status, error, action_id),
            )

    # --- reads ---

    def equity_at_or_before(self, ts_utc: str) -> float | None:
        """Most recent equity snapshot at or before the given UTC ISO timestamp."""
        with self._conn() as c:
            row = c.execute(
                "SELECT equity FROM equity_snapshots WHERE ts_utc <= ? "
                "ORDER BY ts_utc DESC LIMIT 1",
                (ts_utc,),
            ).fetchone()
            return float(row["equity"]) if row else None

    def latest_position_snapshot(self) -> tuple[str | None, list[dict[str, Any]]]:
        with self._conn() as c:
            row = c.execute(
                "SELECT snapshot_ts_utc FROM position_snapshot_meta "
                "ORDER BY snapshot_ts_utc DESC LIMIT 1"
            ).fetchone()
            if not row:
                return None, []
            ts = row["snapshot_ts_utc"]
            rows = c.execute(
                "SELECT symbol, qty, market_value, avg_entry_price "
                "FROM position_snapshots WHERE snapshot_ts_utc = ? "
                "ORDER BY symbol ASC",
                (ts,),
            ).fetchall()
            return ts, [dict(r) for r in rows]

    def latest_pending_action(self, kind: str = "strategy_switch") -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM pending_actions "
                "WHERE kind = ? AND status IN ('pending', 'processing') "
                "ORDER BY id DESC LIMIT 1",
                (kind,),
            ).fetchone()
            if not row:
                return None
            out = dict(row)
            try:
                out["payload"] = json.loads(out.pop("payload_json"))
            except Exception:
                out["payload"] = {}
            return out

    def recent_orders(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
