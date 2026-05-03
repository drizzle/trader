"""Command-line dispatcher.

Usage:
    python -m trader run                # start the live trading bot
    python -m trader backtest           # run a backtest, write HTML report
    python -m trader dashboard          # start the FastAPI dashboard
    python -m trader switch-strategy    # change deployed strategy + restart

For backwards compat, `python -m trader.main` still runs the bot directly.
"""
from __future__ import annotations

import argparse
import inspect
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from loguru import logger


def _strategy_config_path(config_path: str, strategy: str | None) -> str:
    """Prefer config.<strategy>.yaml when a dashboard strategy override is given."""
    if not strategy:
        return config_path

    path = Path(config_path)
    candidate = path.with_name(f"config.{strategy}.yaml")
    if candidate.exists():
        return str(candidate)
    return config_path


def _cmd_run(args: argparse.Namespace) -> int:
    from .main import run
    run()
    return 0


def _cmd_backtest(args: argparse.Namespace) -> int:
    from .backtest import run_backtest
    from .config import load_config
    from .data import DataClient
    from .reports import render_report
    from .strategy import STRATEGIES, build_strategy

    cfg = load_config(_strategy_config_path(args.config, args.strategy))
    logger.remove()
    logger.add(sys.stderr, level=cfg.log_level)

    # Allow CLI override of which strategy to backtest, without editing config.yaml.
    if args.strategy:
        if args.strategy not in STRATEGIES:
            logger.error(
                f"Unknown strategy '{args.strategy}'. Available: {list(STRATEGIES.keys())}"
            )
            return 1
        # Use empty params unless the configured strategy matches the override.
        params = cfg.strategy.params if args.strategy == cfg.strategy.name else {}
        strategy = build_strategy(args.strategy, params)
        strategy_name_for_filename = args.strategy
    else:
        strategy = build_strategy(cfg.strategy.name, cfg.strategy.params)
        strategy_name_for_filename = cfg.strategy.name

    data = DataClient(cfg.alpaca)

    logger.info(
        f"Fetching {args.lookback_days} days of bars for {strategy.universe}..."
    )
    bars = data.daily_bars(strategy.universe, lookback_days=args.lookback_days)
    if not bars or all(df.empty for df in bars.values()):
        logger.error("No bars returned — check API keys and symbol list.")
        return 1

    logger.info(
        f"Running backtest [{strategy.name}]: cash=${args.cash:,.0f} "
        f"slippage={args.slippage_bps}bps commission=${args.commission:.2f}"
        f"{' (risk caps OFF)' if args.no_risk else ''}"
    )
    result = run_backtest(
        strategy=strategy,
        bars=bars,
        initial_cash=args.cash,
        commission=args.commission,
        slippage_bps=args.slippage_bps,
        start=args.start,
        end=args.end,
        risk_config=None if args.no_risk else cfg.risk,
    )

    metrics = result.metrics
    logger.info("=== Backtest metrics ===")
    for k, v in metrics.items():
        logger.info(f"  {k}: {v}")

    out_dir = (cfg.data_dir / "backtests").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.output:
        out_path = Path(args.output)
    else:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_path = out_dir / f"{strategy_name_for_filename}_{ts}.html"

    written = render_report(result, out_path)
    logger.info(f"Report written: {written}")
    print(str(written))   # so callers can pipe / parse
    return 0


def _cmd_switch_strategy(args: argparse.Namespace) -> int:
    """Atomically switch the deployed strategy.

    Designed to be invoked by the dashboard via subprocess so the dashboard
    process never touches the Alpaca trading API directly. The flow:

      1. Engage kill switch (so the running trader skips ticks during the cut).
      2. (optional, --flatten) Connect to Alpaca, close all open positions.
         Wait briefly for cancels/closes to register at the broker.
      3. Atomically rewrite config.yaml: new strategy name, default params,
         and the universe declared by the new strategy class.
      4. (optional, --restart) `sudo systemctl restart trader`.

    The kill switch is intentionally LEFT engaged after the switch — the user
    should verify the new strategy is healthy on /strategy then release on
    /risk to start trading. This is a safety property, not a bug.

    Exit codes (the dashboard parses these to surface failure cause):
      0 ok / 1 bad args / 2 flatten failed / 3 config write failed
      4 systemctl restart failed
    """
    from .config import load_config
    from .strategy import STRATEGIES

    cfg = load_config(args.config)
    logger.remove()
    logger.add(sys.stderr, level="INFO")

    name = args.name
    if name not in STRATEGIES:
        logger.error(
            f"Unknown strategy '{name}'. Available: {sorted(STRATEGIES.keys())}"
        )
        return 1

    # ---- 1. Engage kill switch ---------------------------------------------
    kill_path = Path(cfg.risk.kill_switch_path)
    kill_path.parent.mkdir(parents=True, exist_ok=True)
    kill_path.write_text(
        f"engaged for strategy switch to '{name}' at "
        f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}"
    )
    logger.info(f"[1/4] Kill switch engaged: {kill_path}")

    # ---- 2. Optionally flatten ---------------------------------------------
    if args.flatten:
        try:
            from .execution import ExecutionClient
            from .storage import Storage
            ec = ExecutionClient(cfg.alpaca, Storage(cfg.db_path))
            positions_before = list(ec.positions().keys())
            if positions_before:
                logger.info(f"[2/4] Flattening positions: {positions_before}")
                ec.close_all_positions()
                # Brief wait so close orders register at the broker before we
                # restart the trader process. Not a fill guarantee — Alpaca
                # accepts the requests well within 3s; fills follow.
                time.sleep(3)
            else:
                logger.info("[2/4] No open positions to flatten")
        except Exception as e:
            logger.error(f"[2/4] Flatten failed: {e}")
            logger.error("Aborting switch. Kill switch left engaged.")
            return 2
    else:
        logger.info("[2/4] Skipping flatten (--flatten not passed)")

    # ---- 3. Rewrite config.yaml --------------------------------------------
    strategy_cls = STRATEGIES[name]
    sig = inspect.signature(strategy_cls.__init__)
    default_params: dict = {
        p_name: p.default
        for p_name, p in sig.parameters.items()
        if p_name != "self" and p.default is not inspect.Parameter.empty
    }
    try:
        instance = strategy_cls(**default_params)
        new_universe = list(instance.universe)
    except Exception as e:
        logger.error(f"[3/4] Could not instantiate {name} with defaults: {e}")
        return 3

    config_path = Path(args.config).resolve()
    try:
        raw = yaml.safe_load(config_path.read_text())
    except Exception as e:
        logger.error(f"[3/4] Could not read existing config: {e}")
        return 3

    raw["strategy"] = {"name": name, "params": default_params}
    raw["universe"] = new_universe

    header = (
        "# Strategy + runtime config. Secrets live in .env, NOT here.\n"
        f"# Last updated by switch-strategy at "
        f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
        f"# Active strategy: {name}\n\n"
    )
    body = yaml.safe_dump(raw, sort_keys=False, default_flow_style=False)

    # NOTE: We deliberately write directly to config.yaml instead of the
    # usual tmp-file + atomic-rename pattern. The dashboard service runs with
    # systemd's ProtectSystem=strict, which makes /opt/trader/ read-only
    # except for paths listed in ReadWritePaths. Granting write to the
    # specific file /opt/trader/config.yaml does NOT grant write to its
    # parent directory, so creating a sibling .tmp file fails with EROFS.
    # Direct write has a tiny corruption window (milliseconds for a ~2KB
    # YAML); recovery is `git checkout config.yaml` if it ever happens.
    try:
        config_path.write_text(header + body)
    except Exception as e:
        logger.error(f"[3/4] Could not write new config: {e}")
        return 3
    logger.info(
        f"[3/4] Wrote config: strategy={name}, universe={new_universe}"
    )

    # ---- 4. Restart trader service -----------------------------------------
    if args.restart:
        try:
            r = subprocess.run(
                ["sudo", "-n", "systemctl", "restart", "trader"],
                check=True, capture_output=True, text=True, timeout=30,
            )
            time.sleep(2)
            check = subprocess.run(
                ["sudo", "-n", "systemctl", "is-active", "trader"],
                capture_output=True, text=True, timeout=10,
            )
            state = check.stdout.strip() or "unknown"
            logger.info(f"[4/4] systemctl restart ok — service is {state}")
            if state != "active":
                logger.error(
                    "Service is not active after restart. Check journalctl -u trader."
                )
                return 4
        except subprocess.CalledProcessError as e:
            logger.error(
                f"[4/4] systemctl restart failed: {(e.stderr or '').strip() or e}"
            )
            logger.error(
                "Hint: ensure /etc/sudoers.d/trader-restart is installed "
                "(see deploy/sudoers.d/trader-restart)."
            )
            return 4
        except subprocess.TimeoutExpired:
            logger.error("[4/4] systemctl restart timed out")
            return 4
    else:
        logger.info("[4/4] Skipping restart (--restart not passed)")

    logger.success(
        f"Strategy switched to {name}. Kill switch is engaged — "
        f"release it on /risk when ready to start trading."
    )
    return 0


def _cmd_dashboard(args: argparse.Namespace) -> int:
    import uvicorn
    from .dashboard.app import create_app
    from .config import load_config
    from .strategy import STRATEGIES

    if args.strategy and args.strategy not in STRATEGIES:
        logger.error(
            f"Unknown strategy '{args.strategy}'. Available: {list(STRATEGIES.keys())}"
        )
        return 1

    cfg = load_config(_strategy_config_path(args.config, args.strategy))
    if args.strategy and cfg.strategy.name != args.strategy:
        cfg = cfg.model_copy(
            update={"strategy": cfg.strategy.model_copy(update={"name": args.strategy})}
        )

    app = create_app(cfg)
    uvicorn.run(app, host=args.host, port=args.port, log_level=cfg.log_level.lower())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="trader", description="Personal trading platform.")
    sub = parser.add_subparsers(dest="cmd")

    p_run = sub.add_parser("run", help="Start the live trading bot.")
    p_run.set_defaults(func=_cmd_run)

    p_bt = sub.add_parser("backtest", help="Run a backtest, output an HTML report.")
    p_bt.add_argument("--config", default="config.yaml")
    p_bt.add_argument("--strategy", default=None,
                      help="Strategy name to backtest (overrides config.yaml). "
                           "E.g. sma_crossover, yypt_tqqq_rsi.")
    p_bt.add_argument("--lookback-days", type=int, default=2500,
                      help="How many days of history to fetch (default ~10y).")
    p_bt.add_argument("--start", help="ISO date e.g. 2018-01-01")
    p_bt.add_argument("--end", help="ISO date e.g. 2024-12-31")
    p_bt.add_argument("--cash", type=float, default=100_000.0)
    p_bt.add_argument("--commission", type=float, default=0.0)
    p_bt.add_argument("--slippage-bps", type=float, default=5.0)
    p_bt.add_argument("--no-risk", action="store_true",
                      help="Disable risk caps in backtest (compare with/without).")
    p_bt.add_argument("--output", help="Path to write report (default: data/backtests/<name>.html)")
    p_bt.set_defaults(func=_cmd_backtest)

    p_switch = sub.add_parser(
        "switch-strategy",
        help="Switch deployed strategy: optionally flatten, write config, restart trader.",
    )
    p_switch.add_argument("--config", default="config.yaml")
    p_switch.add_argument("--name", required=True,
                          help="Target strategy name (must be registered).")
    p_switch.add_argument("--flatten", action="store_true",
                          help="Close all open positions before restart.")
    p_switch.add_argument("--restart", action="store_true",
                          help="Run sudo systemctl restart trader after writing config.")
    p_switch.set_defaults(func=_cmd_switch_strategy)

    p_dash = sub.add_parser("dashboard", help="Start the FastAPI dashboard.")
    p_dash.add_argument("--config", default="config.yaml")
    p_dash.add_argument("--host", default="127.0.0.1",
                        help="Bind host. Keep 127.0.0.1 and access via SSH tunnel.")
    p_dash.add_argument("--port", type=int, default=8000)
    p_dash.add_argument("--strategy", default=None,
                        help="Dashboard strategy name. If config.<strategy>.yaml exists, "
                             "that config is loaded.")
    p_dash.set_defaults(func=_cmd_dashboard)

    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
