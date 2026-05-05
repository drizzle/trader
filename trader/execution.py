"""Wraps Alpaca's trading API. Reads positions, places orders, reconciles fills."""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest

# QueryOrderStatus moved between alpaca-py versions; import defensively so
# an outdated dependency doesn't crash the dashboard at startup. We fall back
# to filtering by string status in Python.
try:
    from alpaca.trading.enums import QueryOrderStatus  # type: ignore
    _HAS_QUERY_ORDER_STATUS = True
except ImportError:
    QueryOrderStatus = None  # type: ignore
    _HAS_QUERY_ORDER_STATUS = False

# Order statuses Alpaca reports as "still open at the broker" — i.e. the
# order's notional is still being held against buying power and we should
# NOT fire a duplicate.
_OPEN_STATUSES = {
    "new", "accepted", "pending_new", "partially_filled",
    "pending_cancel", "pending_replace", "accepted_for_bidding",
}
from loguru import logger

from .config import AlpacaConfig
from .storage import Storage


@dataclass
class AccountSnapshot:
    cash: float
    equity: float
    buying_power: float


@dataclass
class Position:
    symbol: str
    qty: float
    market_value: float
    avg_entry_price: float


class ExecutionClient:
    def __init__(self, config: AlpacaConfig, storage: Storage):
        self._client = TradingClient(
            api_key=config.api_key,
            secret_key=config.secret_key,
            paper=not config.live,
        )
        self.storage = storage
        self.live = config.live

    # --- read-only ---

    def account(self) -> AccountSnapshot:
        a = self._client.get_account()
        return AccountSnapshot(
            cash=float(a.cash),
            equity=float(a.equity),
            buying_power=float(a.buying_power),
        )

    def positions(self) -> dict[str, Position]:
        """Return current positions keyed by symbol.

        Index under BOTH symbol forms for crypto. Alpaca's positions endpoint
        historically returns crypto as "BTCUSD" (no slash) on some accounts /
        SDK versions, while the trading endpoint and our strategies use the
        canonical "BTC/USD" form. If our strategy looks up `positions["BTC/USD"]`
        and only finds "BTCUSD", `current_qty` falls to 0 and the trader
        re-issues the full target buy every tick (which is exactly what
        was happening — Alpaca rejected each one for insufficient cash since
        the position was already open).
        """
        out: dict[str, Position] = {}
        for p in self._client.get_all_positions():
            sym = p.symbol
            base_pos = Position(
                symbol=sym,
                qty=float(p.qty),
                market_value=float(p.market_value),
                avg_entry_price=float(p.avg_entry_price),
            )
            out[sym] = base_pos
            # Cross-index the alternate crypto form so lookups work either way.
            if "/" not in sym and sym.endswith("USD") and len(sym) > 3:
                alt = f"{sym[:-3]}/USD"
                out[alt] = Position(
                    symbol=alt,
                    qty=base_pos.qty,
                    market_value=base_pos.market_value,
                    avg_entry_price=base_pos.avg_entry_price,
                )
            elif "/" in sym:
                alt = sym.replace("/", "")
                out[alt] = Position(
                    symbol=alt,
                    qty=base_pos.qty,
                    market_value=base_pos.market_value,
                    avg_entry_price=base_pos.avg_entry_price,
                )
        return out

    def is_market_open(self) -> bool:
        return bool(self._client.get_clock().is_open)

    def open_orders_for(self, symbol: str) -> list:
        """Return open (not-yet-filled / not-yet-cancelled) orders for symbol.

        Used by the tick loop to skip submitting a new order when a previous
        one for the same symbol is still pending at the broker. Without this
        check, every tick that sees `current_qty != target_qty` will fire a
        new order, and Alpaca holds the pending order's notional against your
        buying power — so the second order fails BP at the broker even when
        our local BP cap thought there was room.
        """
        try:
            # Prefer the explicit OPEN filter when available; otherwise fetch
            # the recent window and filter by status string in Python.
            if _HAS_QUERY_ORDER_STATUS:
                req = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=100)
            else:
                req = GetOrdersRequest(limit=100)
            orders = self._client.get_orders(filter=req)
        except Exception as e:
            logger.warning(f"Could not fetch open orders for {symbol}: {e}")
            return []

        # Alpaca crypto symbols use the slash form ("BTC/USD"); equity uses "AAPL".
        result = []
        for o in orders:
            if o.symbol != symbol:
                continue
            # If the broker filter wasn't applied, drop anything terminal.
            status = (getattr(o.status, "value", None) or str(o.status)).lower()
            if not _HAS_QUERY_ORDER_STATUS and status not in _OPEN_STATUSES:
                continue
            result.append(o)
        return result

    # --- writes ---

    def cancel_open_orders(self) -> None:
        logger.warning("Canceling open orders")
        self._client.cancel_orders()

    def submit_market_order(
        self, symbol: str, qty: float, side: OrderSide, strategy: str
    ) -> str | None:
        """Submit a market order. Returns the client_order_id on success, None on failure.

        Idempotent: client_order_id is generated and stored in SQLite. If the bot
        crashes mid-submit, the next tick will diff fresh and won't double-up.
        """
        if qty <= 0:
            logger.debug(f"Skip {side} {symbol}: qty {qty} <= 0")
            return None

        # Alpaca client order IDs must be unique. Truncate uuid for readability in logs.
        client_order_id = f"trader-{uuid.uuid4().hex[:16]}"

        # Crypto symbols look like "BTC/USD". They trade 24/7, so DAY orders
        # would be rejected — they need GTC. Equities still use DAY.
        is_crypto = "/" in symbol
        tif = TimeInForce.GTC if is_crypto else TimeInForce.DAY

        req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            time_in_force=tif,
            client_order_id=client_order_id,
        )

        try:
            order = self._client.submit_order(req)
            self.storage.record_order_submitted(
                client_order_id=client_order_id,
                broker_order_id=str(order.id),
                symbol=symbol,
                side=side.value,
                qty=qty,
                order_type="market",
                limit_price=None,
                strategy=strategy,
            )
            logger.info(
                f"Submitted {side.value} {qty} {symbol} (client_order_id={client_order_id})"
            )
            return client_order_id
        except Exception as e:
            self.storage.record_order_failed(
                client_order_id=client_order_id,
                symbol=symbol,
                side=side.value,
                qty=qty,
                error=str(e),
                strategy=strategy,
            )
            logger.error(f"Order failed for {side.value} {qty} {symbol}: {e}")
            return None

    def reconcile_recent_orders(self, lookback_minutes: int = 60) -> None:
        """Pull recent orders from Alpaca and update local fill status.

        Cheap and good enough for swing trading. V2 should use the streaming
        trade_updates websocket instead.
        """
        from datetime import datetime, timedelta, timezone
        since = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
        req = GetOrdersRequest(after=since, limit=100)
        try:
            orders = self._client.get_orders(filter=req)
        except Exception as e:
            logger.warning(f"Could not fetch recent orders for reconciliation: {e}")
            return

        for o in orders:
            if not o.client_order_id or not o.client_order_id.startswith("trader-"):
                continue
            self.storage.update_order_fill(
                client_order_id=o.client_order_id,
                status=o.status.value,
                filled_qty=float(o.filled_qty or 0),
                filled_avg_price=float(o.filled_avg_price) if o.filled_avg_price else None,
            )

    def close_all_positions(self) -> None:
        """Used by the daily-loss kill path. Cancels open orders + flattens."""
        logger.warning("Flattening all positions")
        try:
            self._client.cancel_orders()
            self._client.close_all_positions(cancel_orders=True)
        except Exception as e:
            logger.error(f"Failed to flatten positions: {e}")
            raise
