"""Entry point. Wires modules together, runs the scheduler, blocks until stopped."""
from __future__ import annotations

import math
import signal
import sys
from datetime import datetime, timezone

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

    if risk.kill_switch_engaged():
        logger.warning("Kill switch engaged — skipping tick")
        return

    # Crypto trades 24/7 — skip the equity-market-hours gate for crypto strategies.
    if not strategy.is_crypto and not execution.is_market_open():
        logger.debug("Market closed — skipping tick")
        return

    # 1. Snapshot account state.
    try:
        account = execution.account()
    except Exception as e:
        logger.error(f"Failed to fetch account: {e}")
        alerts.send(f"⚠️ Trader: failed to fetch account: {e}")
        return

    storage.record_equity(account.cash, account.equity, account.buying_power)
    logger.info(
        f"Account: equity=${account.equity:,.2f} cash=${account.cash:,.2f} "
        f"bp=${account.buying_power:,.2f} mode={'LIVE' if execution.live else 'PAPER'}"
    )

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
    total_target = sum(s.target_pct for s in signals)
    cash = risk.check_cash_buffer(total_target)
    if not cash.allow:
        logger.warning(cash.reason)
        return

    # 5. Diff signals vs current positions, submit orders.
    positions = execution.positions()
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
