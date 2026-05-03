"""Generate a self-contained HTML report from a BacktestResult.

Uses Plotly with `include_plotlyjs='inline'` so the file is fully portable
(no CDN needed). Open it in any browser, email it, archive over time.
"""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from .backtest import BacktestResult


_CSS = """
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       margin: 0; padding: 24px; background: #0e1116; color: #e6edf3; }
.container { max-width: 1200px; margin: 0 auto; }
h1 { font-size: 1.6rem; margin: 0 0 4px; }
.sub { color: #8b949e; font-size: 0.9rem; margin-bottom: 24px; }
.card { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
        padding: 16px; margin-bottom: 20px; }
.card h2 { margin: 0 0 12px; font-size: 1.05rem; color: #c9d1d9; }
.metric-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; }
.metric { background: #0d1117; border: 1px solid #30363d; border-radius: 6px; padding: 12px; }
.metric .label { font-size: 0.78rem; color: #8b949e; text-transform: uppercase; letter-spacing: 0.05em; }
.metric .value { font-size: 1.3rem; font-weight: 600; margin-top: 4px; }
.value.pos { color: #3fb950; }
.value.neg { color: #f85149; }
table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #21262d; }
th { color: #8b949e; font-weight: 500; text-transform: uppercase; font-size: 0.72rem; letter-spacing: 0.05em; }
tr:hover td { background: #1c2128; }
.trade-buy { color: #3fb950; }
.trade-sell { color: #f85149; }
.scroll { max-height: 480px; overflow-y: auto; }
.chart-wrap { width: 100%; overflow-x: hidden; }
@media (max-width: 700px) {
  body { padding: 12px; font-size: 16px; }
  h1 { font-size: 1.25rem; line-height: 1.25; }
  .sub { font-size: 0.9rem; margin-bottom: 14px; }
  .card { padding: 14px; margin-bottom: 14px; border-radius: 6px; }
  .card h2 { font-size: 1.05rem; }
  .metric-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
  .metric { padding: 10px; min-width: 0; }
  .metric .label { font-size: 0.7rem; }
  .metric .value { font-size: 1.05rem; overflow-wrap: anywhere; }
  table { display: block; overflow-x: auto; white-space: nowrap; font-size: 0.9rem; }
  th, td { padding: 10px 8px; }
  .scroll { max-height: none; overflow-x: auto; }
}
"""


def _metric_card(label: str, value: float, suffix: str = "%", color: bool = True) -> str:
    cls = ""
    if color:
        cls = "pos" if value > 0 else ("neg" if value < 0 else "")
    return (
        f'<div class="metric"><div class="label">{escape(label)}</div>'
        f'<div class="value {cls}">{value:+.2f}{suffix}</div></div>'
    )


def _equity_chart(result: BacktestResult) -> str:
    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.05,
        row_heights=[0.7, 0.3],
        subplot_titles=("Equity vs Buy-and-Hold", "Drawdown"),
    )
    fig.add_trace(go.Scatter(
        x=result.equity_curve.index, y=result.equity_curve["equity"],
        name="Strategy", line=dict(color="#58a6ff", width=2),
    ), row=1, col=1)
    if result.benchmark is not None:
        fig.add_trace(go.Scatter(
            x=result.benchmark.index, y=result.benchmark.values,
            name="Buy & Hold", line=dict(color="#8b949e", width=1.5, dash="dot"),
        ), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=result.equity_curve.index,
        y=result.equity_curve["drawdown"] * 100,
        name="Drawdown %", line=dict(color="#f85149", width=1),
        fill="tozeroy", fillcolor="rgba(248,81,73,0.15)",
    ), row=2, col=1)
    fig.update_yaxes(title_text="$", row=1, col=1, gridcolor="#30363d")
    fig.update_yaxes(title_text="%", row=2, col=1, gridcolor="#30363d")
    fig.update_xaxes(gridcolor="#30363d")
    fig.update_layout(
        autosize=True, height=520, template="plotly_dark",
        paper_bgcolor="#161b22", plot_bgcolor="#161b22",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=38, r=12, t=44, b=36),
        font=dict(size=12),
    )
    return fig.to_html(
        include_plotlyjs="inline",
        full_html=False,
        config={"displayModeBar": False, "responsive": True},
    )


def _trade_log_table(result: BacktestResult) -> str:
    if not result.trades:
        return "<p style='color:#8b949e'>No trades.</p>"
    rows = []
    for t in result.trades:
        side_cls = "trade-buy" if t.side == "buy" else "trade-sell"
        rows.append(
            f"<tr><td>{t.timestamp.date()}</td>"
            f"<td>{escape(t.symbol)}</td>"
            f"<td class='{side_cls}'>{t.side.upper()}</td>"
            f"<td>{t.qty}</td>"
            f"<td>${t.price:.2f}</td>"
            f"<td>${t.cash_after:,.2f}</td>"
            f"<td>{escape(t.rationale)}</td></tr>"
        )
    return (
        "<div class='scroll'><table><thead><tr>"
        "<th>Date</th><th>Symbol</th><th>Side</th><th>Qty</th>"
        "<th>Fill</th><th>Cash After</th><th>Rationale</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
    )


def render_report(result: BacktestResult, output_path: str | Path) -> Path:
    """Write a self-contained HTML report. Returns the path written."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    m = result.metrics
    metric_cards = "".join([
        _metric_card("Total Return", m["total_return_pct"]),
        _metric_card("CAGR", m["cagr_pct"]),
        _metric_card("Sharpe", m["sharpe"], suffix="", color=True),
        _metric_card("Max Drawdown", m["max_drawdown_pct"]),
        _metric_card("Volatility (ann.)", m["vol_annualized_pct"], color=False),
        _metric_card("Win Rate", m["win_rate_pct"], color=False),
        _metric_card("Buy & Hold", m["buy_and_hold_return_pct"]),
        _metric_card("Alpha vs B&H", m["alpha_vs_buy_and_hold_pct"]),
    ])
    summary = (
        f"<div><strong>Strategy:</strong> {escape(result.strategy_name)} &nbsp;|&nbsp; "
        f"<strong>Symbols:</strong> {', '.join(result.config.get('symbols', []))} &nbsp;|&nbsp; "
        f"<strong>Period:</strong> {result.start.date()} → {result.end.date()} "
        f"({m['n_days']} days)</div>"
        f"<div><strong>Initial cash:</strong> ${result.initial_cash:,.0f} &nbsp;|&nbsp; "
        f"<strong>Slippage:</strong> {result.config.get('slippage_bps', 0):.1f} bps &nbsp;|&nbsp; "
        f"<strong>Commission:</strong> ${result.config.get('commission', 0):.2f}</div>"
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Backtest — {escape(result.strategy_name)}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="container">
  <h1>Backtest Report — {escape(result.strategy_name)}</h1>
  <div class="sub">Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')}</div>

  <div class="card">{summary}</div>

  <div class="card">
    <h2>Metrics</h2>
    <div class="metric-grid">{metric_cards}</div>
  </div>

  <div class="card">
    <h2>Equity Curve & Drawdown</h2>
    <div class="chart-wrap">{_equity_chart(result)}</div>
  </div>

  <div class="card">
    <h2>Trade Log ({len(result.trades)} trades)</h2>
    {_trade_log_table(result)}
  </div>
</div>
</body>
</html>"""

    output_path.write_text(html, encoding="utf-8")
    return output_path
