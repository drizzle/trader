"""Risk checks. Every signal that wants to become an order goes through here first."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from .config import RiskConfig
from .storage import Storage


@dataclass
class RiskDecision:
    allow: bool
    reason: str = ""


class RiskCheck:
    def __init__(self, config: RiskConfig, storage: Storage):
        self.config = config
        self.storage = storage
        self._kill_switch = Path(config.kill_switch_path)

    def kill_switch_engaged(self) -> bool:
        """If the kill-switch file exists, halt. Lets you stop the bot without SSH."""
        if self._kill_switch.exists():
            logger.warning(f"Kill switch active: {self._kill_switch}")
            return True
        return False

    def check_position_size(
        self, symbol: str, target_pct: float, current_equity: float
    ) -> RiskDecision:
        if target_pct < 0 or target_pct > self.config.max_position_pct:
            return RiskDecision(
                False,
                f"Target {target_pct:.2%} for {symbol} violates max_position_pct "
                f"{self.config.max_position_pct:.2%}",
            )
        return RiskDecision(True)

    def check_daily_loss(self, current_equity: float) -> RiskDecision:
        """Halt if equity has dropped more than daily_loss_limit_pct from start-of-day.

        Start-of-day = the most recent equity snapshot before today 00:00 UTC.
        Crude but works. V2 should use market-open snapshots instead.
        """
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        sod_equity = self.storage.equity_at_or_before(today_start.isoformat(timespec="seconds"))
        if sod_equity is None:
            # No prior snapshot — first day running. Allow.
            return RiskDecision(True)

        loss_pct = (sod_equity - current_equity) / sod_equity
        if loss_pct > self.config.daily_loss_limit_pct:
            return RiskDecision(
                False,
                f"Daily loss {loss_pct:.2%} exceeds limit "
                f"{self.config.daily_loss_limit_pct:.2%}. Halting.",
            )
        return RiskDecision(True)

    def check_cash_buffer(self, target_total_pct: float) -> RiskDecision:
        if target_total_pct > 1.0 - self.config.min_cash_buffer_pct:
            return RiskDecision(
                False,
                f"Targeted exposure {target_total_pct:.2%} would breach cash buffer "
                f"{self.config.min_cash_buffer_pct:.2%}",
            )
        return RiskDecision(True)

    def cap_qty_to_buying_power(
        self,
        qty: float,
        price: float,
        buying_power: float,
        is_crypto: bool = False,
        slippage_buffer_pct: float = 0.005,
    ) -> tuple[float, str | None]:
        """Cap a BUY order's qty to fit available buying power.

        Returns (capped_qty, info_message). info_message is None if no cap was
        needed; otherwise it explains why we sized down (or refused entirely).

        Why this exists separately from `check_position_size` / `check_cash_buffer`:
        those are *portfolio*-level caps (max % per symbol, aggregate cash buffer).
        This is *order*-level economics — the actual dollar cost of THIS order
        vs the broker's view of available buying power.

        Why a slippage buffer:
        - We compute qty from the last bar's close, but the broker prices the
          order against the next live quote. Crypto especially can move 0.2-1.0%
          in the seconds between strategy tick and order arrival.
        - For Alpaca crypto, buying_power == cash (no margin), so an order that
          looks like it costs 99% of cash will be rejected by overshoot.
        Default 0.5% buffer is conservative for liquid crypto / equities.
        """
        if qty <= 0 or price <= 0:
            return qty, None
        estimated_cost = qty * price * (1.0 + slippage_buffer_pct)
        if estimated_cost <= buying_power:
            return qty, None

        max_affordable_raw = buying_power / (price * (1.0 + slippage_buffer_pct))
        capped = round(max_affordable_raw, 8) if is_crypto else float(math.floor(max_affordable_raw))
        if capped <= 0:
            return 0.0, (
                f"Insufficient buying power: need ~${estimated_cost:,.2f}, "
                f"have ${buying_power:,.2f} (not enough for one unit at "
                f"${price:,.2f}+{slippage_buffer_pct:.1%} slippage buffer)"
            )
        return capped, (
            f"Capped order qty {qty} → {capped} to fit buying power "
            f"${buying_power:,.2f} (full size would cost ~${estimated_cost:,.2f})"
        )
