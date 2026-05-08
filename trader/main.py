"""Entry point. Wires modules together, runs the scheduler, blocks until stopped."""
from __future__ import annotations

import math
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from alpaca.trading.enums import OrderSide
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from .alerts import Alerts
from .config import Config, load_config
from .data import DataClient
from .execution import ExecutionClient
from .risk import RiskCheck
from .storage import Storage
from .strategy import Signal, Strategy, build_strategy


def _position_snapshot_rows(positions: dict[str, object]) -> list[dict[str, float | str]]:
    values = list(positions.values())
    symbols = {getattr(p, "symbol") for p in values}
    rows: list[dict[str, float | str]] = []
    seen: set[str] = set()
    for p in values:
        symbol = getattr(p, "symbol")
        if (
            "/" not in symbol
            and symbol.endswith("USD")
            and f"{symbol[:-3]}/USD" in symbols
        ):
            continue
        if symbol in seen:
            continue
        seen.add(symbol)
        rows.append({
            "symbol": symbol,
            "qty": float(getattr(p, "qty")),
            "market_value": float(getattr(p, "market_value")),
            "avg_entry_price": float(getattr(p, "avg_entry_price")),
        })
    return rows


def _unique_positions(positions: dict[str, object]) -> list[object]:
    values = list(positions.values())
    symbols = {getattr(p, "symbol") for p in values}
    out: list[object] = []
    seen: set[str] = set()
    for p in values:
        symbol = getattr(p, "symbol")
        if (
            "/" not in symbol
            and symbol.endswith("USD")
            and f"{symbol[:-3]}/USD" in symbols
        ):
            continue
        if symbol in seen:
            continue
        seen.add(symbol)
        out.append(p)
    return out


def _enforce_cash_buffer(
    *,
    cfg: Config,
    account: object,
    positions: dict[str, object],
    execution: ExecutionClient,
    storage: Storage,
    strategy: Strategy,
    alerts: Alerts,
) -> bool:
    required_cash = float(account.equity) * cfg.risk.min_cash_buffer_pct
    cash_shortfall = required_cash - float(account.cash)
    if cash_shortfall <= 1.0:
        return False

    unique = _unique_positions(positions)
    if not unique:
        logger.warning(
            f"Cash buffer breach: cash=${account.cash:,.2f}, "
            f"required=${required_cash:,.2f}, but no positions are open to trim"
        )
        return False

    strategy_symbols = set(strategy.universe)
    ordered = sorted(
        unique,
        key=lambda p: (
            getattr(p, "symbol") in strategy_symbols,
            -float(getattr(p, "market_value")),
        ),
    )
    for p in ordered:
        symbol = getattr(p, "symbol")
        pending_broker = execution.open_orders_for(symbol)
        pending_local = storage.open_orders(symbol)
        if pending_broker or pending_local:
            logger.info(
                f"Cash buffer breach: cash=${account.cash:,.2f}, "
                f"required=${required_cash:,.2f}; waiting for existing open order(s) "
                f"on {symbol} (broker={len(pending_broker)}, local={len(pending_local)})"
            )
            return True

    logger.warning(
        f"Cash buffer breach: cash=${account.cash:,.2f}, required=${required_cash:,.2f}. "
        f"Trimming about ${cash_shortfall:,.2f} of exposure."
    )
    alerts.send(
        f"⚠️ *Cash buffer breach*\nCash ${account.cash:,.2f} is below required "
        f"${required_cash:,.2f}. Trimming positions."
    )

    remaining = cash_shortfall
    submitted = False
    for p in ordered:
        symbol = getattr(p, "symbol")
        qty = float(getattr(p, "qty"))
        market_value = float(getattr(p, "market_value"))
        if qty <= 0 or market_value <= 0 or remaining <= 1.0:
            continue
        price = market_value / qty
        if price <= 0:
            continue
        if "/" in symbol:
            sell_qty = min(qty, round(remaining / price, 8))
            if sell_qty < 0.0001:
                continue
        else:
            sell_qty = min(qty, float(math.ceil(remaining / price)))
            if sell_qty < 1:
                continue
        order_id = execution.submit_market_order(
            symbol=symbol,
            qty=sell_qty,
            side=OrderSide.SELL,
            strategy=f"{strategy.name}:cash_buffer",
        )
        if order_id:
            submitted = True
            remaining -= sell_qty * price
            alerts.send(
                f"📉 *Cash-buffer trim submitted*\nSELL {sell_qty} {symbol} "
                f"@ ~${price:,.2f}"
            )

    if submitted:
        return True
    logger.warning("Cash buffer remains breached, but no trim orders were submitted")
    return False


def _scale_signals_for_cash_buffer(cfg: Config, signals: list[Signal]) -> list[Signal]:
    """Reduce aggregate exposure to fit the configured cash buffer.

    The backtester already does this, and live trading should behave the same
    way: a 95% Composer allocation with a 10% cash buffer becomes 90% exposure
    instead of skipping every buy order.
    """
    total_target = sum(s.target_pct for s in signals)
    max_total = max(0.0, 1.0 - cfg.risk.min_cash_buffer_pct)
    if total_target <= max_total or total_target <= 0:
        return signals

    scale = max_total / total_target if max_total > 0 else 0.0
    logger.warning(
        f"Targeted exposure {total_target:.2%} would breach cash buffer "
        f"{cfg.risk.min_cash_buffer_pct:.2%}; scaling targets by {scale:.3f}"
    )
    return [
        Signal(
            s.symbol,
            s.target_pct * scale,
            f"{s.rationale} | scaled by {scale:.3f} for cash buffer",
        )
        if s.target_pct > 0
        else s
        for s in signals
    ]


def _process_pending_strategy_switch(
    *,
    storage: Storage,
    execution: ExecutionClient,
    cfg: Config,
) -> bool:
    pending = storage.latest_pending_action("strategy_switch")
    if not pending:
        return False

    payload = pending.get("payload", {})
    name = str(payload.get("name") or "")
    if not name:
        storage.mark_pending_action(pending["id"], "failed", "missing target strategy")
        return False

    if not execution.is_market_open():
        logger.info(f"Strategy switch to {name} is staged; waiting for market open")
        return True

    storage.mark_pending_action(pending["id"], "processing")
    cmd = [
        sys.executable, "-m", "trader", "switch-strategy",
        "--account", cfg.account.id,
        "--name", name,
    ]
    if payload.get("restart", True):
        cmd.append("--restart")
    if payload.get("flatten", True):
        cmd.append("--flatten")

    env = os.environ.copy()
    env["TRADER_LOAD_DOTENV"] = "true"
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(Path(__file__).resolve().parents[1]),
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
        )
    except Exception as e:
        storage.mark_pending_action(pending["id"], "pending", str(e))
        logger.error(f"Staged strategy switch to {name} failed to launch: {e}")
        return True

    if proc.returncode == 0:
        storage.mark_pending_action(pending["id"], "completed")
        logger.info(f"Completed staged strategy switch to {name}")
    else:
        detail = (proc.stderr or proc.stdout or "").strip()[-500:]
        storage.mark_pending_action(pending["id"], "pending", detail)
        logger.error(f"Staged strategy switch to {name} failed rc={proc.returncode}: {detail}")
    return True


def _setup_logging(cfg: Config) -> None:
    logger.remove()
    logger.add(sys.stderr, level=cfg.log_level)
    log_path = cfg.data_dir / "trader.log"
    logger.add(
        log_path,
        rotation="10 MB",
        retention="30 days",
        level=cfg.log_level,
        enqueue=True,
    )
    logger.info(f"Logging to {log_path}")


def tick(
    cfg: Config,
    strategy: Strategy,
    data_client: DataClient,
    execution: ExecutionClient,
    risk: RiskCheck,
    storage: Storage,
    alerts: Alerts,
) -> None:
    """One pass of the trading loop. Called by the scheduler."""
    logger.debug("--- tick start ---")

    # 1. Snapshot broker state for the dashboard. This is read-only and runs
    # even while halted, so the dashboard does not need Alpaca credentials.
    try:
        account = execution.account()
        positions = execution.positions()
    except Exception as e:
        logger.error(f"Failed to fetch account: {e}")
        alerts.send(f"⚠️ Trader: failed to fetch account: {e}")
        return

    storage.record_equity(account.cash, account.equity, account.buying_power)
    storage.record_position_snapshot(_position_snapshot_rows(positions))
    logger.info(
        f"Account: equity=${account.equity:,.2f} cash=${account.cash:,.2f} "
        f"bp=${account.buying_power:,.2f} mode={'LIVE' if execution.live else 'PAPER'}"
    )
    # Include prior-day DAY orders so the dashboard/local pending-order view
    # does not leave expired or filled orders stuck as "submitted".
    execution.reconcile_recent_orders(lookback_minutes=3 * 24 * 60)

    if _process_pending_strategy_switch(storage=storage, execution=execution, cfg=cfg):
        return

    if risk.kill_switch_engaged():
        logger.warning("Kill switch engaged — skipping tick")
        return

    # Crypto trades 24/7 — skip the equity-market-hours gate for crypto strategies.
    if not strategy.is_crypto and not execution.is_market_open():
        logger.debug("Market closed — skipping tick")
        return

    # 2. Daily-loss circuit breaker.
    daily = risk.check_daily_loss(account.equity)
    if not daily.allow:
        logger.error(daily.reason)
        alerts.send(f"🛑 *Daily loss limit hit*\n{daily.reason}\nFlattening positions.")
        execution.close_all_positions()
        # Engage kill switch to require manual reset.
        kill_path = risk._kill_switch
        kill_path.parent.mkdir(parents=True, exist_ok=True)
        kill_path.write_text(f"engaged at {datetime.now(timezone.utc).isoformat()} — daily loss")
        return

    if _enforce_cash_buffer(
        cfg=cfg,
        account=account,
        positions=positions,
        execution=execution,
        storage=storage,
        strategy=strategy,
        alerts=alerts,
    ):
        return

    # 3. Fetch bars and compute signals.
    try:
        if strategy.is_crypto:
            bars = data_client.crypto_daily_bars(strategy.universe, lookback_days=400)
        else:
            bars = data_client.daily_bars(strategy.universe, lookback_days=400)
    except Exception as e:
        logger.error(f"Failed to fetch bars: {e}")
        alerts.send(f"⚠️ Trader: failed to fetch bars: {e}")
        return

    signals: list[Signal] = strategy.compute(bars)
    for s in signals:
        storage.record_signal(strategy.name, s.symbol, s.target_pct, s.rationale)
        logger.info(f"Signal: {s.symbol} target={s.target_pct:.2%} ({s.rationale})")

    # 4. Cash-buffer guard on aggregate exposure.
    signals = _scale_signals_for_cash_buffer(cfg, signals)
    total_target = sum(s.target_pct for s in signals)
    cash = risk.check_cash_buffer(total_target)
    if not cash.allow:
        logger.warning(cash.reason)
        return

    # 5. Diff signals vs current positions, submit orders.
    last_prices = {sym: float(df["close"].iloc[-1]) for sym, df in bars.items() if len(df)}

    for s in signals:
        # Risk check per-symbol.
        check = risk.check_position_size(s.symbol, s.target_pct, account.equity)
        if not check.allow:
            logger.warning(check.reason)
            continue

        price = last_prices.get(s.symbol)
        if price is None or price <= 0:
            logger.warning(f"No price for {s.symbol}, skipping")
            continue

        target_dollars = account.equity * s.target_pct
        if strategy.is_crypto:
            # Fractional crypto: keep float qty, round to 8 decimals (Alpaca's
            # precision for BTC). Skip dust below the typical 0.0001 BTC min.
            target_qty = round(target_dollars / price, 8)
            current_qty = float(positions[s.symbol].qty) if s.symbol in positions else 0.0
            diff = round(target_qty - current_qty, 8)
            if abs(diff) < 0.0001:
                logger.debug(
                    f"{s.symbol}: already near target ({current_qty} ≈ {target_qty})"
                )
                continue
        else:
            target_qty = math.floor(target_dollars / price)  # whole shares for equities
            current_qty = int(positions[s.symbol].qty) if s.symbol in positions else 0
            diff = target_qty - current_qty
            if diff == 0:
                logger.debug(f"{s.symbol}: already at target ({current_qty} shares)")
                continue

        side = OrderSide.BUY if diff > 0 else OrderSide.SELL
        order_qty = abs(diff)

        # Idempotency: if there's already an open order for this symbol at
        # the broker, skip. Without this check we re-submit every tick (since
        # current_qty stays at zero until fills land), and Alpaca holds each
        # pending order's notional against BP — so duplicates get rejected.
        pending = execution.open_orders_for(s.symbol)
        if pending:
            logger.info(
                f"{s.symbol}: skipping — {len(pending)} order(s) still pending "
                f"at broker (ids: {[o.client_order_id for o in pending]})"
            )
            continue

        # Buying-power gate — only meaningful for BUYs. SELLs reduce exposure
        # and don't consume cash.
        if side == OrderSide.BUY:
            # For crypto, cap against `cash`, NOT `buying_power`. Alpaca's
            # buying_power for paper accounts with stock margin enabled is
            # 2x cash — but crypto can't be bought on margin, so the broker's
            # actual constraint is settled cash. Using BP here causes the
            # exact rejection pattern we hit:
            #   "insufficient balance for USD (requested: 9811, available: 4903)"
            # i.e. ordering 2x what's actually spendable.
            #
            # Bumped buffer to 5% for crypto so a 1-2% adverse move between
            # bar close and Alpaca's live ask still leaves slack.
            if strategy.is_crypto:
                available = account.cash
                slippage_buffer = 0.05
            else:
                available = account.buying_power
                slippage_buffer = 0.005
            capped_qty, cap_msg = risk.cap_qty_to_buying_power(
                order_qty, price, available,
                is_crypto=strategy.is_crypto,
                slippage_buffer_pct=slippage_buffer,
            )
            if capped_qty <= 0:
                logger.warning(f"{s.symbol}: {cap_msg}")
                alerts.send(f"⚠️ *Order skipped* {s.symbol}: {cap_msg}")
                continue
            if cap_msg:
                # Sized down — log but don't alert (would be too noisy on a
                # busy day). The order itself will still emit its alert below.
                logger.warning(f"{s.symbol}: {cap_msg}")
            order_qty = capped_qty

        order_id = execution.submit_market_order(
            symbol=s.symbol, qty=order_qty, side=side, strategy=strategy.name
        )
        if order_id:
            alerts.send(
                f"📈 *Order submitted*\n{side.value.upper()} {order_qty} {s.symbol} "
                f"@ ~${price:.2f}\n{s.rationale}"
            )

    # 6. Reconcile fills from recent orders.
    execution.reconcile_recent_orders(lookback_minutes=60)
    logger.debug("--- tick end ---")


def run() -> None:
    """Entry point invoked by `python -m trader.main` or the `trader` console script."""
    cfg = load_config()
    _setup_logging(cfg)

    logger.info(f"=== trader v0.1 starting ({'LIVE' if cfg.alpaca.live else 'PAPER'}) ===")
    if cfg.alpaca.live:
        logger.warning("LIVE TRADING ENABLED — real money at risk")

    storage = Storage(cfg.db_path)
    data_client = DataClient(cfg.alpaca)
    execution = ExecutionClient(cfg.alpaca, storage)
    risk = RiskCheck(cfg.risk, storage)
    alerts = Alerts(cfg.telegram)
    strategy = build_strategy(cfg.strategy.name, cfg.strategy.params)

    alerts.send(
        f"✅ Trader started ({'LIVE' if cfg.alpaca.live else 'PAPER'}) — "
        f"strategy={cfg.strategy.name}, interval={cfg.schedule.interval_minutes}m"
    )

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        tick,
        IntervalTrigger(minutes=cfg.schedule.interval_minutes),
        kwargs={
            "cfg": cfg,
            "strategy": strategy,
            "data_client": data_client,
            "execution": execution,
            "risk": risk,
            "storage": storage,
            "alerts": alerts,
        },
        id="trader_tick",
        max_instances=1,           # don't overlap if a tick runs long
        coalesce=True,             # if we miss ticks (e.g. restart), only run once
        misfire_grace_time=300,
        next_run_time=datetime.now(timezone.utc),  # run once immediately on startup
    )

    def _shutdown(signum, frame):
        logger.info(f"Received signal {signum}, shutting down")
        alerts.send("⏹ Trader shutting down")
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    run()
