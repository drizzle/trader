"""Market-summary feature: 7 canned LLM prompts.

Important caveat baked into the system prompt: DeepSeek (or any LLM) does
NOT have real-time news access. We disclose this in every response so the
user doesn't mistake hallucinated current events for fact.

For the prompts that lend themselves to real-time data (1, 2, 4, 6, 7), we
optionally inject snapshot price stats from Alpaca for the universe symbols
so the LLM has at least the latest closing data to ground its narrative.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .advisors.base import MarketContext
from .advisors.llm import DeepSeekClient


@dataclass
class SummaryPrompt:
    key: str
    label: str
    template: str          # may contain {tickers} or {topic} placeholders
    placeholder_kind: str = "none"   # 'none' | 'tickers' | 'topic'
    needs_market_data: bool = False
    placeholder_default: str = ""


PROMPTS: list[SummaryPrompt] = [
    SummaryPrompt(
        key="todays_market",
        label="Today's market in plain English",
        template=(
            "Summarize today's stock market in plain English — include major movers and why. "
            "Use any market data provided below for grounding; if none is provided or it's stale, "
            "say so. Do not invent specific news or quotes."
        ),
        needs_market_data=True,
    ),
    SummaryPrompt(
        key="weekly_breakdown",
        label="Weekly breakdown of selected ETFs/stocks",
        template=(
            "Give me a weekly breakdown of {tickers} with key insights. "
            "Focus on trends and what changed, not noise. Use the market data below for grounding."
        ),
        placeholder_kind="tickers",
        placeholder_default="SPY, QQQ",
        needs_market_data=True,
    ),
    SummaryPrompt(
        key="bull_bear_cases",
        label="Analyst bull and bear cases for a company",
        template=(
            "What might analysts be saying about {topic}? Summarize both BULL and BEAR cases "
            "in two clearly-labeled sections. Be balanced. Note that you don't have real-time "
            "analyst reports — present the strongest version of each side based on what's "
            "publicly understood about the company."
        ),
        placeholder_kind="topic",
        placeholder_default="NVDA",
    ),
    SummaryPrompt(
        key="watchlist_daily",
        label="Daily price moves + key context for a watchlist",
        template=(
            "For the tickers {tickers}, give me a brief daily-style summary: "
            "price move (use the data below), what's likely driving it (be honest about uncertainty), "
            "and any structural context worth knowing. Keep each ticker to 3-4 sentences."
        ),
        placeholder_kind="tickers",
        placeholder_default="AAPL, MSFT, NVDA, TSLA, GOOGL",
        needs_market_data=True,
    ),
    SummaryPrompt(
        key="explain_event",
        label="Explain a financial event like I'm not a finance major",
        template=(
            "Explain '{topic}' like I'm not a finance major. Define jargon, "
            "give me the cause-and-effect, and tell me why it matters. "
            "Note that your knowledge has a cutoff — if the event is very recent, say so."
        ),
        placeholder_kind="topic",
        placeholder_default="the inverted yield curve",
    ),
    SummaryPrompt(
        key="sector_momentum",
        label="Sectors gaining momentum right now",
        template=(
            "What sectors are likely gaining momentum right now? Group by sector "
            "(tech, financials, energy, healthcare, etc.), give a 1-2 sentence rationale "
            "for each, and acknowledge that you don't have real-time data — base it on "
            "any market data provided plus broader knowledge of recent macro trends."
        ),
        needs_market_data=True,
    ),
    SummaryPrompt(
        key="weekly_recap",
        label="2-minute weekly market recap",
        template=(
            "Turn the available context into a weekly market recap I can read in under 2 minutes. "
            "Structure: (1) Headline takeaway, (2) Top 3 things that happened, "
            "(3) What to watch next week. Be concise. Don't invent specifics."
        ),
        needs_market_data=True,
    ),
]


PROMPTS_BY_KEY: dict[str, SummaryPrompt] = {p.key: p for p in PROMPTS}


_SYSTEM_PROMPT = """\
You are a market-summary assistant. Your knowledge has a cutoff — you do NOT
have real-time news, social media, or analyst reports. When asked about
'today' or 'right now', say what you can responsibly say based on the data
provided plus general macro understanding, and clearly flag uncertainty.

Never fabricate specific news headlines, analyst quotes, or numerical claims
you can't support from the data block below. If you don't know, say so.

Use clear plain English. Markdown formatting is fine.
"""


def render_prompt(prompt: SummaryPrompt, user_input: str | None = None) -> str:
    """Substitute the user's input into the template for {tickers} or {topic}."""
    text = prompt.template
    if prompt.placeholder_kind == "tickers":
        text = text.replace("{tickers}", user_input or prompt.placeholder_default)
    elif prompt.placeholder_kind == "topic":
        text = text.replace("{topic}", user_input or prompt.placeholder_default)
    return text


def build_user_prompt(
    prompt: SummaryPrompt,
    user_input: str | None,
    contexts: list[MarketContext] | None = None,
) -> str:
    """Combine the rendered prompt + optional market-data block."""
    base = render_prompt(prompt, user_input)
    if not prompt.needs_market_data or not contexts:
        return base

    blocks = "\n\n".join(c.to_prompt_block() for c in contexts)
    return (
        f"{base}\n\n"
        f"--- LATEST MARKET DATA (from Alpaca) ---\n{blocks}\n"
        f"-----------------------------------------"
    )


def run_summary(
    prompt: SummaryPrompt,
    user_input: str | None,
    llm: DeepSeekClient,
    contexts: list[MarketContext] | None = None,
    max_tokens: int = 900,
) -> str:
    """Returns the LLM's response as Markdown text."""
    user = build_user_prompt(prompt, user_input, contexts)
    return llm.chat(
        system=_SYSTEM_PROMPT,
        user=user,
        max_tokens=max_tokens,
        temperature=0.4,
    )
