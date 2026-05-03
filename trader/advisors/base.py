"""Advisor abstract base + shared dataclasses.

An Advisor is a pure function of (symbol, MarketContext) → Recommendation.
The MarketContext is built ONCE per symbol per refresh cycle from Alpaca data,
then reused across all advisors — saves API calls and keeps prompts consistent.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from .llm import DeepSeekClient, LLMError


@dataclass
class MarketContext:
    """Numeric facts about a symbol that we feel safe sending to the LLM."""
    symbol: str
    as_of: str                    # ISO timestamp
    last_close: float
    return_1d_pct: float | None
    return_1w_pct: float | None
    return_1m_pct: float | None
    return_3m_pct: float | None
    return_1y_pct: float | None
    fifty_two_week_high: float | None
    fifty_two_week_low: float | None
    drawdown_from_high_pct: float | None
    realized_vol_20d_pct: float | None
    realized_vol_60d_pct: float | None
    rsi_14: float | None
    sma_50: float | None
    sma_200: float | None
    avg_volume_20d: float | None

    def to_prompt_block(self) -> str:
        """Render as a compact, LLM-friendly text block."""
        def fmt(v, suffix=""):
            return f"{v:.2f}{suffix}" if v is not None else "n/a"
        lines = [
            f"Symbol: {self.symbol}",
            f"As-of: {self.as_of}",
            f"Last close: ${fmt(self.last_close)}",
            f"Returns: 1d {fmt(self.return_1d_pct, '%')}, "
            f"1w {fmt(self.return_1w_pct, '%')}, "
            f"1m {fmt(self.return_1m_pct, '%')}, "
            f"3m {fmt(self.return_3m_pct, '%')}, "
            f"1y {fmt(self.return_1y_pct, '%')}",
            f"52w range: ${fmt(self.fifty_two_week_low)} – ${fmt(self.fifty_two_week_high)} "
            f"(drawdown {fmt(self.drawdown_from_high_pct, '%')})",
            f"Realized vol (annualized): 20d {fmt(self.realized_vol_20d_pct, '%')}, "
            f"60d {fmt(self.realized_vol_60d_pct, '%')}",
            f"RSI(14): {fmt(self.rsi_14)}",
            f"SMA50: ${fmt(self.sma_50)}, SMA200: ${fmt(self.sma_200)}",
            f"Avg daily volume (20d): {fmt(self.avg_volume_20d)}",
        ]
        return "\n".join(lines)


@dataclass
class Recommendation:
    advisor: str
    symbol: str
    action: str            # 'BUY' / 'HOLD' / 'SELL' / 'ERROR'
    confidence: float      # 0.0 - 1.0
    rationale: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# Strict JSON schema we ask the LLM to return. Keeps parsing simple.
_RESPONSE_SPEC = """\
Respond with a SINGLE JSON object (no markdown, no prose) of the form:
{
  "action": "BUY" | "HOLD" | "SELL",
  "confidence": <number between 0 and 1>,
  "rationale": "<2-4 sentences explaining your reasoning>"
}
"""


class Advisor(ABC):
    """Subclass to add a new persona. Must define `name`, `style_summary`,
    and `system_prompt()`. Optionally override `user_prompt()` to inject
    persona-specific framing."""

    name: str = "unnamed"
    style_summary: str = ""

    @abstractmethod
    def system_prompt(self) -> str: ...

    def user_prompt(self, context: MarketContext) -> str:
        return (
            f"Provide your investment recommendation for the symbol below "
            f"based on the data provided. You only have public price data — "
            f"acknowledge that limitation in your rationale if relevant.\n\n"
            f"--- MARKET DATA ---\n{context.to_prompt_block()}\n\n"
            f"{_RESPONSE_SPEC}"
        )

    def recommend(self, context: MarketContext, llm: DeepSeekClient) -> Recommendation:
        try:
            raw = llm.chat(
                system=self.system_prompt(),
                user=self.user_prompt(context),
                response_format_json=True,
                max_tokens=400,
                temperature=0.3,
            )
            parsed = json.loads(raw)
            action = str(parsed.get("action", "HOLD")).upper()
            if action not in {"BUY", "HOLD", "SELL"}:
                action = "HOLD"
            confidence = float(parsed.get("confidence", 0.0))
            confidence = max(0.0, min(1.0, confidence))
            rationale = str(parsed.get("rationale", "")).strip()[:1200]
            return Recommendation(
                advisor=self.name,
                symbol=context.symbol,
                action=action,
                confidence=confidence,
                rationale=rationale,
            )
        except (LLMError, ValueError, json.JSONDecodeError) as e:
            return Recommendation(
                advisor=self.name,
                symbol=context.symbol,
                action="ERROR",
                confidence=0.0,
                rationale="",
                error=f"{type(e).__name__}: {e}",
            )
