# Trading Platform — Architecture Plan

**Scope:** A 24/7 server-based platform to execute swing/position-trading strategies on US equities and ETFs.
**Approach:** Build the smallest thing that works end-to-end, then harden it in well-defined stages.
**Out of scope (for now):** strategy design, alpha research, sub-second latency, options/futures.

> ⚠️ This is a **technical platform plan**, not investment advice. The system runs whatever logic you give it. Profitability depends entirely on the strategies and risk rules you implement. Always run new strategies on a paper-trading account for weeks (ideally months) before risking real capital.

---

## 1. Stack recommendation (opinionated)

| Layer | Recommendation | Why |
|---|---|---|
| **Broker / execution API** | [Alpaca](https://alpaca.markets) | Free paper-trading account, clean Python SDK (`alpaca-py`), commission-free, REST + websocket, great for v1. IBKR is more powerful but much harder to build against. |
| **Market data (v1)** | Alpaca Market Data API | Bundled with the broker account. IEX feed is free; SIP feed is ~$99/mo if you need full-market quotes. |
| **Language** | Python 3.11+ | Best ecosystem for quant/backtesting. |
| **Scheduling** | [APScheduler](https://apscheduler.readthedocs.io/) | In-process cron. Triggers your strategy at market open, every N minutes, etc. |
| **Storage (v1)** | SQLite | Zero-setup, file-based. Plenty for a single-user system tracking trades + signals. Upgrade to Postgres when you outgrow it. |
| **Backtesting** | [vectorbt](https://vectorbt.dev) or [backtrader](https://www.backtrader.com) | vectorbt = fast, NumPy-based. backtrader = more event-driven, closer to live execution. |
| **Logging** | Loguru | One-line setup, structured logs. |
| **Process supervision** | systemd | Auto-restart on crash, runs at boot. |
| **Hosting (v1)** | DigitalOcean droplet or Hetzner CX11 | $5–8/month. Pick a region close to Alpaca's API endpoints (us-east). |
| **Alerts** | Telegram bot or email via SMTP | Push critical events (orders filled, errors, daily P&L) to your phone. |

---

## 2. Build in three stages

The key principle: **get a paper-trading bot end-to-end before adding any complexity.** Most projects die because people build a "perfect" architecture before they've made a single trade.

### Stage 1 — V1: "It executes a strategy on paper"
**Goal:** A single Python process running 24/7 on a VM that reads market data, applies strategy logic, and submits paper orders to Alpaca. Logs everything. You get a Telegram ping when it trades.

### Stage 2 — V2: "It's observable and recoverable"
**Goal:** Add a dashboard, real backtesting pipeline, persistent metrics, proper alerting, and Dockerization. Move from paper to small live capital once you've validated a strategy for weeks.

### Stage 3 — V3: "It scales to multiple strategies"
**Goal:** Decouple services (data, signals, execution, risk), add a risk manager, support multiple strategies running in parallel, optionally move to container orchestration.

---

## 3. V1 architecture (minimum viable)

```
┌──────────────────────────────────────────────────────────┐
│  Cloud VM (Ubuntu 22.04, $6/mo DigitalOcean droplet)     │
│                                                           │
│  systemd ──► python -m trader  (single long-running proc)│
│                │                                          │
│                ├── APScheduler (triggers strategy ticks) │
│                │                                          │
│                ├── DataClient ──► Alpaca Market Data API │
│                │                                          │
│                ├── Strategy ──► generates signals        │
│                │                                          │
│                ├── RiskCheck ──► position size, max DD   │
│                │                                          │
│                ├── ExecutionClient ──► Alpaca Trading API│
│                │                                          │
│                ├── SQLite ──► trades, signals, positions │
│                │                                          │
│                └── AlertBot ──► Telegram                 │
└──────────────────────────────────────────────────────────┘
```

### Project structure

```
trader/
├── pyproject.toml
├── config.yaml              # API keys via env vars, not committed
├── trader/
│   ├── __init__.py
│   ├── main.py              # entry point — sets up scheduler, runs forever
│   ├── data.py              # DataClient: wraps Alpaca data API
│   ├── execution.py         # ExecutionClient: places orders, tracks fills
│   ├── strategy/
│   │   ├── base.py          # Strategy ABC
│   │   └── sma_crossover.py # Example: 50/200 SMA crossover on SPY
│   ├── risk.py              # RiskCheck: max position size, daily loss limit
│   ├── storage.py           # SQLite models + queries
│   ├── alerts.py            # Telegram bot
│   └── scheduler.py         # APScheduler setup
├── backtests/
│   └── sma_crossover_2015_2024.ipynb
├── tests/
└── deploy/
    ├── trader.service       # systemd unit
    └── deploy.sh
```

### The execution loop (conceptually)

For a swing strategy, you don't need real-time data. A simple loop runs every 5–15 minutes during market hours:

1. Scheduler fires → "tick" event.
2. DataClient fetches latest bars for the universe (e.g. SPY, QQQ, top-50 S&P).
3. Strategy computes signals (e.g. "SPY is in an uptrend, target 100% allocation").
4. RiskCheck filters signals (position size, max drawdown, daily loss limit, cash buffer).
5. ExecutionClient diffs current portfolio vs. target → submits orders.
6. Storage records signals, orders, fills.
7. AlertBot pings you on any order, error, or end-of-day summary.

### Hard rules to bake in from day one
- **Always paper-trade first.** Use Alpaca's paper environment by default. Live trading requires an explicit env-var flag.
- **Idempotency.** If the bot restarts mid-tick, it should not double-submit orders. Use client order IDs.
- **Kill switch.** A flag file (e.g. `/etc/trader/STOP`) that, if present, exits cleanly without placing trades. So you can halt the system without SSH'ing in.
- **Daily loss limit.** Hard-coded ceiling. If breached, flatten positions and refuse new orders until manually reset.
- **All times in UTC** internally. Convert to America/New_York only when checking market hours.
- **Secrets in env vars**, never in code or config files.

---

## 4. Hosting decision (V1)

**Recommendation: DigitalOcean Basic Droplet, 1 vCPU / 1 GB RAM, NYC region. $6/month.**

Why a VM over containers for v1:
- One process, one machine — nothing to orchestrate.
- SSH in, `tail -f logs`, restart with `systemctl restart trader`. Debuggable.
- Closer to Alpaca's us-east API endpoints than a typical serverless platform.
- Bills predictably. No surprise invoices.

Why a cloud VM over your own machine:
- Your home wifi / power outages = missed trades or stuck positions.
- Cloud VMs have ~99.99% uptime. A swing strategy can tolerate 30 min of downtime; missing market open by 4 hours because your router rebooted is not OK.

When to graduate to containers (Stage 2): when you have 2+ services (e.g. trader + dashboard + worker), or want zero-downtime deploys.

---

## 5. V2 improvements (after V1 has run on paper for 2–4 weeks)

1. **Dockerize.** One `Dockerfile`, one `docker-compose.yml`. Makes redeploys reproducible.
2. **Add a dashboard.** FastAPI + a simple HTMX or React frontend showing: open positions, today's signals, trade log, P&L curve, system health. Hosted on the same VM.
3. **Move SQLite → Postgres.** Once you want to query history without locking up the live process.
4. **Backtesting pipeline.** A separate command (`python -m trader backtest --strategy sma_crossover --from 2015-01-01`) that reuses your live strategy code on historical data. **Critical:** the strategy code must be identical between backtest and live. If you re-implement logic, you'll have backtest/live divergence.
5. **Metrics + alerting.** Push key numbers (latency, equity, drawdown, error count) to a free service like [healthchecks.io](https://healthchecks.io) and [betterstack.com](https://betterstack.com). They page you when the bot stops checking in.
6. **Walk-forward validation.** Before going live, run the strategy on out-of-sample data. Most strategies that look great in-sample fail here.
7. **Go live with small capital.** Start with the smallest amount you'd be sad to lose. Run live + paper in parallel for at least a month and verify they agree.

---

## 6. V3 architecture (multi-strategy, hardened)

```
                  ┌─────────────────┐
                  │  Risk Manager   │ ◄─── single source of truth for
                  │  (caps, limits) │      portfolio risk + sizing
                  └────────┬────────┘
                           ▲
            ┌──────────────┼──────────────┐
            │              │              │
    ┌───────┴────┐  ┌──────┴─────┐  ┌────┴────────┐
    │ Strategy A │  │ Strategy B │  │ Strategy C  │  ◄─ each is a worker
    └─────┬──────┘  └─────┬──────┘  └─────┬───────┘     subscribed to data
          │               │               │
          └───────┬───────┴───────┬───────┘
                  ▼               ▼
          ┌───────────────┐  ┌──────────────┐
          │ Data Service  │  │ Execution    │
          │ (websockets)  │  │ Service      │
          └───────┬───────┘  └──────┬───────┘
                  │                 │
          ┌───────┴────┐    ┌───────┴────┐
          │  Postgres  │    │   Broker   │
          │ + TimescaleDB│  │  (Alpaca)  │
          └────────────┘    └────────────┘
```

Key changes from V2:
- **Risk Manager is centralized** — every order goes through it. No strategy can blow past portfolio limits.
- **Services communicate over a message bus** (Redis pub/sub or NATS). Strategies emit signals; execution consumes them.
- **Time-series DB** for tick/bar data (TimescaleDB extension for Postgres).
- **Container orchestration** — Docker Compose on a bigger VM, or Fly.io / ECS if you want managed.

Don't build V3 prematurely. Most retail trading systems never need it.

---

## 7. What I'd build next (concrete first commits)

If you want, I can scaffold V1 right now in your `Trading/` folder:

1. `pyproject.toml` with dependencies pinned (alpaca-py, apscheduler, loguru, pyyaml, pytest).
2. `trader/main.py` skeleton with config loading, scheduler, and a graceful-shutdown handler.
3. `trader/data.py` and `trader/execution.py` wrapping Alpaca's paper endpoints.
4. `trader/strategy/sma_crossover.py` as a reference strategy (50/200-day SMA on SPY — it's a textbook example, not a recommendation).
5. `trader/storage.py` with SQLite tables for `signals`, `orders`, `fills`, `positions_snapshot`.
6. `trader/risk.py` with three hard checks: max position size, daily loss limit, kill-switch file.
7. `deploy/trader.service` systemd unit + `deploy/deploy.sh` for first-time setup.
8. A `README.md` with the exact commands to: create an Alpaca paper account, set env vars, run locally, deploy to a droplet.

After that, your job is to:
- Open an Alpaca paper account (free, ~5 minutes).
- Spin up a $6/mo droplet.
- Run `deploy.sh`.
- Watch it trade on paper for a few weeks before touching real money.

---

## 8. Costs (V1)

| Item | Cost |
|---|---|
| DigitalOcean droplet (1 vCPU, 1GB) | $6/mo |
| Alpaca paper trading | $0 |
| Alpaca live trading + IEX data | $0 |
| Domain name (optional, for dashboard) | ~$12/yr |
| Telegram bot | $0 |
| **Total** | **~$6/month** |

Live trading with the SIP feed (full-market data) adds ~$99/mo, but you don't need it for swing strategies on liquid names.

---

## 9. Things that will bite you if you skip them

- **Time zones.** Alpaca returns timestamps in UTC; market hours are NY time; daylight saving shifts matter.
- **Partial fills.** Your order for 100 shares may fill as 47 + 53 across two seconds. Reconcile against actual fills, not requested quantity.
- **Corporate actions.** Splits, dividends, ticker changes silently break strategies that hardcode share counts or look back across the event.
- **Survivorship bias in backtests.** Your S&P 500 list today is not the S&P 500 list from 2010. Use point-in-time constituent data or your backtests will lie to you.
- **Look-ahead bias.** Using bar `close` to make decisions for that same bar = cheating. Decide on bar `t` using data through bar `t-1`.
- **API rate limits.** Alpaca caps you at 200 req/min on the free tier. Cache aggressively.
- **The strategy itself.** No architecture compensates for a losing strategy. Spend more time on backtesting and paper validation than on infra.
