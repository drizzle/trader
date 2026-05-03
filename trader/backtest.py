"""Backtest engine.

Replays historical bars through the SAME Strategy.compute() that runs live.
This is the whole point: no behavioral drift between backtest and production.

Modeling assumptions (deliberately conservative):
- Decisions on bar t-1's close, fills at bar t's open + slippage. No look-ahead.
- Whole-share orders only.
- Slippage applied as a fraction of price (default 5 bps each side).
- Commissions configurable (default 0 since Alpaca is commission-free for stocks).
- No borrow costs, no margin, long-only.
- Cash earns 0 interest.

This is a single-asset-friendly backtester. Multi-asset works but allocates
strictly per-signal (no portfolio-level optimization).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from .config import RiskConfig
from .strategy import Strategy


@dataclass
class Trade:
    timestamp: pd.Timestamp
    symbol: str
    side: str               # 'buy' / 'sell'
    qty: int
    price: float            # fill price (includes slippage)
    cash_after: float
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "symbol": self.symbol,
            "side": self.side,
            "qty": self.qty,
            "price": round(self.price, 4),
            "cash_after": round(self.cash_after, 2),
            "rationale": self.rationale,
        }


@dataclass
class BacktestResult:
    strategy_name: str
    start: pd.Timestamp
    end: pd.Timestamp
    initial_cash: float
    config: dict[str, Any]
    equity_curve: pd.DataFrame   # columns: equity, cash, positions_value, drawdown
    trades: list[Trade] = field(default_factory=list)
    benchmark: pd.Series | None = None   # buy-and-hold of first symbol, normalized

    @property
    def metrics(self) -> dict[str, float]:
        eq = self.equity_curve["equity"]
        returns = eq.pct_change().dropna()
        n_days = len(eq)
        years = n_days / 252.0 if n_days > 1 else 1.0

        total_return = eq.iloc[-1] / eq.iloc[0] - 1
        cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1 if years > 0 else 0.0
        vol_ann = returns.std() * math.sqrt(252) if len(returns) > 1 else 0.0
        sharpe = (returns.mean() * 252) / (returns.std() * math.sqrt(252)) if returns.std() > 0 else 0.0
        max_dd = self.equity_curve["drawdown"].min()  # already negative

        # Per-trade win rate. A "trade" here = a buy followed by the matching sell.
        wins, losses, n_round_trips = self._round_trip_stats()
        win_rate = wins / n_round_trips if n_round_trips > 0 else 0.0

        bench_return = (
            self.benchmark.iloc[-1] / self.benchmark.iloc[0] - 1
            if self.benchmark is not None and len(self.benchmark) > 1
            else 0.0
        )

        return {
            "total_return_pct": round(total_return * 100, 2),
            "cagr_pct": round(cagr * 100, 2),
            "vol_annualized_pct": round(vol_ann * 100, 2),
            "sharpe": round(sharpe, 2),
            "max_drawdown_pct": round(max_dd * 100, 2),
            "n_trades": len(self.trades),
            "n_round_trips": n_round_trips,
            "win_rate_pct": round(win_rate * 100, 2),
            "buy_and_hold_return_pct": round(bench_return * 100, 2),
            "alpha_vs_buy_and_hold_pct": round((total_return - bench_return) * 100, 2),
            "n_days": n_days,
        }

    def _round_trip_stats(self) -> tuple[int, int, int]:
        """FIFO match buys to sells. Each closed round-trip is profitable or not."""
        wins = losses = 0
        # symbol -> list of (qty_remaining, entry_price)
        open_lots: dict[str, list[list[float]]] = {}
        for t in self.trades:
            if t.side == "buy":
                open_lots.setdefault(t.symbol, []).append([t.qty, t.price])
            else:  # sell
                qty_to_close = t.qty
                lots = open_lots.get(t.symbol, [])
                while qty_to_close > 0 and lots:
                    lot_qty, entry = lots[0]
                    take = min(lot_qty, qty_to_close)
                    pnl = (t.price - entry) * take
                    if pnl > 0:
                        wins += 1
                    else:
                        losses += 1
                    lot_qty -= take
                    qty_to_close -= take
                    if lot_qty == 0:
                        lots.pop(0)
                    else:
                        lots[0][0] = lot_qty
        return wins, losses, wins + losses


def run_backtest(
    strategy: Strategy,
    bars: dict[str, pd.DataFrame],
    initial_cash: float = 100_000.0,
    commission: float = 0.0,
    slippage_bps: float = 5.0,
    start: str | datetime | None = None,
    end: str | datetime | None = None,
    risk_config: RiskConfig | None = None,
) -> BacktestResult:
    """Run a daily-bar backtest. Returns a BacktestResult.

    `bars` must be a {symbol: DataFrame} where each DataFrame has at minimum
    'open' and 'close' columns and a DatetimeIndex.

    If `risk_config` is provided, the backtest enforces:
      - max_position_pct (caps each per-symbol target)
      - min_cash_buffer_pct (reduces aggregate target if it would breach)
    Daily-loss limit + kill-switch are not modeled — they're runtime concerns.
    """
    # Build a unified date index across all symbols (intersection, so every symbol has a price).
    common_index = None
    for df in bars.values():
        idx = df.index
        common_index = idx if common_index is None else common_index.intersection(idx)
    if common_index is None or len(common_index) == 0:
        raise ValueError("No overlapping dates across symbols")

    # Match the timezone-awareness of the bars' index when filtering by date.
    # Alpaca returns UTC-aware timestamps; user-supplied start/end are naive ISO strings.
    index_tz = common_index.tz

    def _coerce(ts) -> pd.Timestamp:
        ts = pd.Timestamp(ts)
        if index_tz is not None and ts.tz is None:
            ts = ts.tz_localize(index_tz)
        elif index_tz is None and ts.tz is not None:
            ts = ts.tz_convert(None)
        return ts

    common_index = common_index.sort_values()
    start_ts = _coerce(start) if start is not None else None
    end_ts = _coerce(end) if end is not None else None

    report_index = common_index
    if start_ts is not None:
        report_index = report_index[report_index >= start_ts]
    if end_ts is not None:
        report_index = report_index[report_index <= end_ts]
    if len(report_index) < 1:
        raise ValueError("Date range too narrow after filtering")

    # Restrict each symbol's DataFrame to the shared range, but keep pre-start
    # history so indicators can warm up before the requested report period.
    bars = {sym: df.loc[common_index].sort_index() for sym, df in bars.items()}

    cash = initial_cash
    positions: dict[str, int] = {sym: 0 for sym in bars}
    equity_rows: list[dict[str, Any]] = []
    trades: list[Trade] = []

    slip = slippage_bps / 10_000.0

    # Loop over dates. At each date t:
    #   1. Decisions are based on bars up to t-1 (no look-ahead).
    #   2. Orders fill at t's open + slippage.
    #   3. End-of-day equity uses t's close.
    for date_t in report_index:
        i = common_index.get_loc(date_t)
        if i == 0:
            continue

        # Slice history up to date_t-1 for the strategy.
        past_bars = {
            sym: df.iloc[: i]   # exclusive of t, inclusive of t-1
            for sym, df in bars.items()
        }
        signals = strategy.compute(past_bars)

        # --- Risk-manager pass (matches what live execution enforces) ---
        if risk_config is not None:
            # Cap per-symbol targets at max_position_pct.
            capped: list = []
            for s in signals:
                if s.target_pct > risk_config.max_position_pct:
                    capped.append(type(s)(
                        s.symbol, risk_config.max_position_pct,
                        f"{s.rationale} | capped to max_position_pct",
                    ))
                else:
                    capped.append(s)
            signals = capped

            # Cap aggregate exposure to leave the cash buffer.
            total = sum(s.target_pct for s in signals)
            max_total = 1.0 - risk_config.min_cash_buffer_pct
            if total > max_total and total > 0:
                scale = max_total / total
                signals = [
                    type(s)(s.symbol, s.target_pct * scale,
                            f"{s.rationale} | scaled by {scale:.3f} for cash buffer")
                    for s in signals
                ]

        # Mark current portfolio at t's OPEN (pre-fill, for accurate equity calc).
        for s in signals:
            if s.symbol not in bars:
                continue
            row_t = bars[s.symbol].iloc[i]
            open_t = float(row_t["open"])
            if open_t <= 0 or pd.isna(open_t):
                continue

            # Total equity at t's open (cash + market value at open).
            mv_at_open = sum(
                positions[sym] * float(bars[sym].iloc[i]["open"])
                for sym in positions
            )
            equity_at_open = cash + mv_at_open
            target_dollars = equity_at_open * s.target_pct
            target_shares = math.floor(target_dollars / open_t)
            current_shares = positions.get(s.symbol, 0)
            diff = target_shares - current_shares

            if diff == 0:
                continue

            if diff > 0:  # buy
                fill_price = open_t * (1 + slip)
                cost = fill_price * diff + commission
                if cost > cash:
                    # Scale down to what we can afford.
                    affordable = math.floor((cash - commission) / fill_price)
                    if affordable <= 0:
                        continue
                    diff = affordable
                    cost = fill_price * diff + commission
                cash -= cost
                positions[s.symbol] = current_shares + diff
                trades.append(Trade(date_t, s.symbol, "buy", diff, fill_price, cash, s.rationale))
            else:  # sell
                qty = -diff
                fill_price = open_t * (1 - slip)
                proceeds = fill_price * qty - commission
                cash += proceeds
                positions[s.symbol] = current_shares - qty
                trades.append(Trade(date_t, s.symbol, "sell", qty, fill_price, cash, s.rationale))

        # End-of-day mark-to-market at t's close.
        positions_value = sum(
            positions[sym] * float(bars[sym].iloc[i]["close"])
            for sym in positions
        )
        equity = cash + positions_value
        equity_rows.append({
            "date": date_t,
            "equity": equity,
            "cash": cash,
            "positions_value": positions_value,
        })

    if not equity_rows:
        raise ValueError("Backtest produced no rows — check input data")

    eq_df = pd.DataFrame(equity_rows).set_index("date")
    running_max = eq_df["equity"].cummax()
    eq_df["drawdown"] = eq_df["equity"] / running_max - 1.0  # negative or zero

    # Buy-and-hold benchmark on the first symbol, sized to initial_cash.
    first_sym = next(iter(bars))
    benchmark_prices = bars[first_sym].loc[eq_df.index, "close"]
    benchmark_norm = benchmark_prices / benchmark_prices.iloc[0] * initial_cash

    return BacktestResult(
        strategy_name=strategy.name,
        start=eq_df.index[0],
        end=eq_df.index[-1],
        initial_cash=initial_cash,
        config={
            "commission": commission,
            "slippage_bps": slippage_bps,
            "symbols": list(bars.keys()),
        },
        equity_curve=eq_df,
        trades=trades,
        benchmark=benchmark_norm,
    )
