"""LLM-powered investment advisor module.

Loose port of the IDEA from https://github.com/virattt/ai-hedge-fund — implemented
from scratch in our codebase for two reasons:
  1. SECURITY: nothing leaves the droplet except minimal stats sent to DeepSeek.
     No third-party Python service ever sees the Alpaca API key.
  2. LICENSE: the upstream repo is AGPL; copying it into a private repo creates
     compliance overhead. Re-implementation gives us a clean MIT-style codebase.

What this module produces:
  - Per-symbol BUY / HOLD / SELL recommendations from N "advisor" personas.
  - A short rationale per recommendation.
  - Optional: a one-prompt market-summary feature (see trader.market_summary).

What it never does:
  - Submit orders. Advisors are advisory only — execution is human-in-the-loop.
  - Send the Alpaca API key, account state, positions, or any user PII to
    DeepSeek. Prompts contain only ticker symbols + computed price stats.
"""
from __future__ import annotations

from .base import Advisor, MarketContext, Recommendation
from .llm import DeepSeekClient, LLMError
from .personas import ADVISORS, build_default_advisors

__all__ = [
    "Advisor", "MarketContext", "Recommendation",
    "DeepSeekClient", "LLMError",
    "ADVISORS", "build_default_advisors",
]
