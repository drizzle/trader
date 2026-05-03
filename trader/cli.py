"""Command-line dispatcher.

Usage:
    python -m trader run            # start the live trading bot (default; same as before)
    python -m trader backtest       # run a backtest, write HTML report
    python -m trader dashboard      # start the FastAPI dashboard

For backwards compat, `python -m trader.main` still runs the bot directly.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

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
