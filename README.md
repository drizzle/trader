# trader — V1

A minimal personal trading bot. Runs swing strategies on US equities via Alpaca, 24/7 on a cloud VM, with a SQLite log of every signal/order/fill and optional Telegram alerts.

> ⚠️ **Defaults to paper.** Live trading requires explicitly setting `ALPACA_LIVE=true` in `.env`. Always validate any new strategy on paper for weeks before flipping the switch.

---

## What's in here

```
.
├── ARCHITECTURE.md         # the design rationale + V2/V3 plan
├── pyproject.toml          # Python deps
├── config.yaml             # strategy + runtime config (no secrets)
├── .env.example            # secrets template — copy to .env and fill in
├── trader/
│   ├── __main__.py         # entry: python -m trader <subcommand>
│   ├── cli.py              # CLI dispatcher: run | backtest | dashboard
│   ├── main.py             # live bot: scheduler + tick loop
│   ├── backtest.py         # backtest engine
│   ├── reports.py          # standalone HTML report (Plotly charts)
│   ├── config.py           # config loader (env + yaml → typed pydantic models)
│   ├── data.py             # Alpaca market-data wrapper
│   ├── execution.py        # Alpaca trading wrapper (orders, positions, account)
│   ├── risk.py             # kill switch, daily-loss limit, position-size caps
│   ├── storage.py          # SQLite: signals, orders, fills, equity snapshots
│   ├── alerts.py           # Telegram alerts (optional)
│   ├── strategy/
│   │   ├── base.py         # Strategy ABC — produces target allocations
│   │   └── sma_crossover.py# reference 50/200 SMA strategy on SPY
│   └── dashboard/
│       ├── app.py          # FastAPI app (read-only)
│       └── templates/      # Jinja2 templates for the 3 tabs
├── tests/                  # smoke tests
└── deploy/
    ├── trader.service              # systemd unit for the live bot
    ├── trader-dashboard.service    # systemd unit for the dashboard
    ├── setup.sh                    # first-time droplet provisioning
    └── update.sh                   # pull + restart both services
```

---

## Local development

```bash
# 1. Set up
python3 -m venv .venv   # Python 3.10+ required
source .venv/bin/activate
pip install -e ".[dev]"

# 2. Configure
cp .env.example .env
# Open .env in your editor and paste your Alpaca PAPER keys.
# Leave ALPACA_LIVE=false.

# 3. Run a single tick to smoke-test
python -m trader.main
# Ctrl-C to stop.

# 4. Run tests
pytest
```

You should see log lines like:
```
=== trader v0.1 starting (PAPER) ===
Account: equity=$100,000.00 cash=$100,000.00 bp=$200,000.00 mode=PAPER
Signal: SPY target=95.00% (long: SMA50=... > SMA200=...)
Submitted buy 198 SPY (client_order_id=trader-...)
```

---

## Deploying to your DigitalOcean droplet

Your droplet doesn't need anything special — Ubuntu 22.04 or 24.04, at least 1 GB RAM. The deploy is git-based: push code from your laptop, pull it on the droplet.

### One-time setup

**1. Push this code to a private GitHub repo.**

```bash
cd /Users/simonliu/Documents/Claude/Projects/Trading
git init
git add .
git commit -m "v0.1 scaffold"
gh repo create trader --private --source=. --push
# (or use the GitHub UI to create the repo and add it as a remote)
```

**2. SSH into the droplet and run setup.**

```bash
ssh root@<your-droplet-ip>

# Clone the repo (use a deploy key or PAT for private repos)
git clone https://github.com/<you>/trader.git /opt/trader

# Run the setup script
sudo bash /opt/trader/deploy/setup.sh
```

The setup script will:
- Install Python 3.11, git, and prerequisites
- Create a `trader` system user (no shell, can't be logged into)
- Create a virtualenv at `/opt/trader/.venv` and install dependencies
- Install the systemd unit at `/etc/systemd/system/trader.service`

**3. Create your `.env` on the droplet.**

```bash
sudo cp /opt/trader/.env.example /opt/trader/.env
sudo nano /opt/trader/.env
# Paste your Alpaca PAPER keys. Keep ALPACA_LIVE=false.

# Lock it down — secrets file should only be readable by trader user
sudo chown trader:trader /opt/trader/.env
sudo chmod 600 /opt/trader/.env
```

**4. Start the service.**

```bash
sudo systemctl enable --now trader
sudo journalctl -u trader -f
```

You should see the same startup logs as locally. The bot is now running 24/7. systemd will restart it on crash and on boot.

### Updating after code changes

On your laptop:
```bash
git add . && git commit -m "tweak strategy" && git push
```

On the droplet:
```bash
sudo bash /opt/trader/deploy/update.sh
```

That's it — pulls latest, reinstalls deps, restarts the service.

### Common ops

| Action | Command |
|---|---|
| Tail live logs | `sudo journalctl -u trader -f` |
| Last 200 log lines | `sudo journalctl -u trader -n 200` |
| Stop the service | `sudo systemctl stop trader` |
| Start the service | `sudo systemctl start trader` |
| Restart | `sudo systemctl restart trader` |
| Status | `sudo systemctl status trader` |
| **Halt trading without stopping the process** | `sudo -u trader touch /opt/trader/data/STOP` |
| Resume after halt | `sudo rm /opt/trader/data/STOP && sudo systemctl restart trader` |
| Inspect SQLite | `sqlite3 /opt/trader/data/trader.db '.schema'` then `select * from orders order by id desc limit 10;` |

### Going live (paper → real money)

1. Confirm at least 2–4 weeks of clean paper-trading logs.
2. Verify the SQLite trade log against your Alpaca dashboard — they must match.
3. On the droplet:
   ```bash
   sudo nano /opt/trader/.env
   # Replace paper API keys with LIVE keys.
   # Change ALPACA_LIVE=true.
   sudo systemctl restart trader
   ```
4. Watch the next few ticks like a hawk. Have the kill switch command ready.
5. Start with the smallest amount of capital you'd be sad to lose.

---

## Backtesting

Run a backtest against historical Alpaca bars. Same `Strategy.compute()` runs in backtest and live, so what you backtest is what you trade.

**Locally:**
```bash
python -m trader backtest --strategy sma_crossover --start 2018-01-01 --end 2024-12-31
# → writes data/backtests/sma_crossover_<timestamp>.html
# Open in your browser to see equity curve, drawdown, metrics, trade log.

python -m trader backtest --strategy yypt_tqqq_rsi --start 2018-01-01 --end 2024-12-31
# → writes data/backtests/yypt_tqqq_rsi_<timestamp>.html
```

**On the droplet:**
```bash
cd /opt/trader
sudo -u trader .venv/bin/python -m trader backtest --strategy yypt_tqqq_rsi --start 2018-01-01
# Reports go to /opt/trader/data/backtests/ and appear in the dashboard's Backtests tab.
```

Useful flags: `--strategy sma_crossover`, `--strategy yypt_tqqq_rsi`, `--cash 50000`, `--slippage-bps 10`, `--commission 0.50`, `--lookback-days 3650`.

The HTML reports are fully self-contained (Plotly inlined) — you can email them, archive them, diff strategy variants over time.

## Live dashboard

A FastAPI app on the droplet shows three views: current strategy state, trades + P&L since deployment, and a list of saved backtest reports. Bound to 127.0.0.1 only — access via SSH tunnel from your laptop.

**On your laptop:**
```bash
ssh -L 8000:localhost:8000 root@<droplet-ip>
# leave that terminal open, then in your browser:
open http://localhost:8000
```

While the SSH session is alive, the dashboard is reachable at `http://localhost:8000`. Close the SSH session and it's gone — no public exposure.

If you want to access it without re-tunneling each time, add this to `~/.ssh/config` on your laptop:
```
Host trader
  HostName <droplet-ip>
  User root
  LocalForward 8000 localhost:8000
```
Then `ssh trader` does the tunnel + login in one step.

## Adding a new strategy

1. Create `trader/strategy/my_strategy.py` subclassing `Strategy`.
2. Register it in `trader/strategy/__init__.py`.
3. Set `strategy.name: my_strategy` in `config.yaml` and pass any constructor args under `strategy.params`.
4. Restart the service.

The contract: `compute(bars)` takes a dict of `{symbol: DataFrame}` and returns a list of `Signal(symbol, target_pct, rationale)`. The execution layer diffs targets against current positions and submits orders. Strategies do not place orders themselves — keeps them backtestable and safe.

---

## What this V1 explicitly does NOT do

- Backtesting. Add a `python -m trader backtest` command in V2 that reuses `Strategy.compute()` against historical bars.
- A web dashboard. Add FastAPI in V2.
- Real-time data via websockets. Daily/15-min polling is fine for swing strategies.
- Options, futures, multi-leg orders.
- Multi-strategy capital allocation.
- Anything fancy with risk (no portfolio vol targeting, no correlation-aware sizing, no drawdown management beyond a hard daily cap).

These are V2/V3 concerns. See `ARCHITECTURE.md`.

---

## License

Personal use. Don't run someone else's trading code with real money without reading every line.
