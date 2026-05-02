"""Risk checks. Every signal that wants to become an order goes through here first."""
from __future__ import annotations

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
