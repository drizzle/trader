# trader

Personal swing- and crypto-trading bot. Runs strategies on US equities and
Alpaca-supported crypto pairs (e.g. BTC/USD), 24/7 on a cloud VM, with a
SQLite log of every signal/order/fill, a FastAPI dashboard, and optional
Telegram alerts.

> ⚠️ **Defaults to paper.** Live trading requires explicitly setting
> `ALPACA_LIVE=true` in `.env`. Always validate any new strategy on paper for
> weeks before flipping the switch.

---

## What's in here

```
.
├── ARCHITECTURE.md         # design rationale + V2/V3 plan
├── pyproject.toml          # Python deps
├── config.yaml             # strategy + runtime config (no secrets)
├── .env.example            # secrets template — copy to .env and fill in
├── trader/
│   ├── __main__.py         # entry: python -m trader <subcommand>
│   ├── cli.py              # subcommand dispatch (run / backtest / dashboard /
│   │                       #   switch-strategy / kill-switch)
│   ├── main.py             # live bot: scheduler + tick loop
│   ├── backtest.py         # backtest engine
│   ├── reports.py          # standalone HTML report (Plotly charts)
│   ├── config.py           # config loader (env + yaml → pydantic models)
│   ├── data.py             # Alpaca market-data wrapper (stocks + crypto)
│   ├── execution.py        # Alpaca trading wrapper (orders, positions, account)
│   ├── risk.py             # kill switch, daily-loss, position caps,
│   │                       #   buying-power gate (caps qty to fit BP)
│   ├── storage.py          # SQLite: signals, orders, fills, equity snapshots
│   ├── alerts.py           # Telegram alerts (optional)
│   ├── indicators.py       # shared TA primitives (SMA, RSI, ...)
│   ├── strategy/
│   │   ├── base.py         # Strategy ABC — produces target allocations
│   │   ├── sma_crossover.py    # 50/200 SMA on SPY (reference)
│   │   ├── btc_sma.py          # 20/50 SMA on BTC/USD (crypto, 24/7)
│   │   └── yypt_tqqq_rsi.py    # YYPT TQQQ/SHV decision tree (Composer port)
│   └── dashboard/
│       ├── app.py          # FastAPI app — Trades / Strategy / Risk /
│       │                   #   Backtests / Advisor / Summary tabs
│       └── templates/      # Jinja2 templates per tab
├── tests/
└── deploy/
    ├── trader.service              # systemd unit for the live bot
    ├── trader-dashboard.service    # systemd unit for the dashboard
    ├── sudoers.d/trader-restart    # narrow grant: dashboard can restart trader
    ├── setup.sh                    # first-time droplet provisioning
    └── update.sh                   # pull + reinstall units + restart services
```

---

## CLI reference

All commands are exposed as `python -m trader <subcommand>`. On the droplet,
prefix with the venv path and `sudo -u trader`:

```bash
sudo -u trader /opt/trader/.venv/bin/python -m trader <subcommand> [opts]
```

For local dev (with the venv active), just `python -m trader <subcommand>`.

### `run` — start the live bot

The default. Runs the strategy from `config.yaml` on a scheduler until killed.
Normally invoked by `systemctl start trader`, not by hand.

```bash
python -m trader run
```

### `dashboard` — start the FastAPI UI

```bash
python -m trader dashboard --host 127.0.0.1 --port 8000
```

On the droplet, `trader-dashboard.service` does this for you. Access via SSH
tunnel (see "Live dashboard" below).

Optional flags:
- `--config <path>` — alternate YAML
- `--strategy <name>` — preview a different strategy without editing config

### `switch-strategy` — change the deployed strategy

Same command the dashboard's Deploy button shells out to.

```bash
# Full deploy: flatten existing positions + restart the trader service
python -m trader switch-strategy --name btc_sma --flatten --restart

# Switch without flattening (let new strategy inherit current portfolio)
python -m trader switch-strategy --name yypt_tqqq_rsi --restart

# Dry run: write new config + flatten, but don't restart yet
python -m trader switch-strategy --name btc_sma --flatten
cat config.yaml                       # eyeball it
sudo systemctl restart trader         # commit when ready

# Switch back to the equity SMA strategy
python -m trader switch-strategy --name sma_crossover --flatten --restart
```

What it does, in order:

1. Engages the kill switch (running trader skips ticks during the cut)
2. (`--flatten`) Closes all open positions via Alpaca, waits ~3s for the
   broker to register the cancels/closes
3. Atomically rewrites `config.yaml` with new strategy name, default params,
   and the universe declared by the strategy class
4. (`--restart`) Runs `sudo systemctl restart trader`
5. **Leaves kill switch engaged.** Verify the new strategy looks sane on
   `/strategy`, then release with `kill-switch release` (below).

Exit codes:

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | bad args (unknown strategy name, etc.) |
| 2 | flatten failed — kill switch left engaged for safety |
| 3 | config write failed |
| 4 | systemctl restart failed (usually missing/wrong sudoers entry) |

### `kill-switch` — halt or resume trading

Engaging just writes a flag file (default `data/STOP`). The trader checks for
it at the top of every tick and skips order placement if present. Positions
are NOT touched — engaging just stops *new* orders.

```bash
# Check current state
python -m trader kill-switch status

# Halt — bot keeps running but won't place orders
python -m trader kill-switch engage --reason "investigating a weird fill"

# Resume
python -m trader kill-switch release
```

`rm /opt/trader/data/STOP` is exactly equivalent to `kill-switch release`.

### `backtest` — historical evaluation

Same `Strategy.compute()` runs in backtest and live, so what you backtest is
what you trade.

```bash
# Default config, ~10y of history
python -m trader backtest

# Specific strategy + date range
python -m trader backtest --strategy btc_sma --start 2020-01-01 --end 2025-01-01

# Compare with vs. without risk caps
python -m trader backtest --strategy sma_crossover
python -m trader backtest --strategy sma_crossover --no-risk

# Custom starting capital, slippage, commission
python -m trader backtest --strategy yypt_tqqq_rsi --cash 50000 --slippage-bps 10 --commission 0.50

# Specify output file
python -m trader backtest --strategy btc_sma --output /tmp/btc_backtest.html
```

Reports land in `data/backtests/<strategy>_<timestamp>.html` and show up on
the dashboard's Backtests tab.

### List available strategies

There's no dedicated subcommand; one-liner via Python:

```bash
sudo -u trader /opt/trader/.venv/bin/python -c \
  "from trader.strategy import STRATEGIES; print('\n'.join(sorted(STRATEGIES)))"
```

### Suggested droplet aliases

Drop these into root's `~/.bashrc` to save typing:

```bash
alias trader-cli='cd /opt/trader && sudo -u trader /opt/trader/.venv/bin/python -m trader'
alias trader-logs='sudo journalctl -u trader -f'
alias trader-status='sudo systemctl status trader trader-dashboard --no-pager'
alias trader-update='sudo bash /opt/trader/deploy/update.sh'
```

Then:

```bash
trader-cli switch-strategy --name btc_sma --flatten --restart
trader-cli kill-switch release
trader-cli kill-switch status
trader-cli backtest --strategy btc_sma --start 2022-01-01
trader-logs
trader-status
trader-update
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
| Errors only, last hour | `sudo journalctl -u trader -p err --since "1 hour ago"` |
| Stop / start / restart service | `sudo systemctl {stop,start,restart} trader` |
| Status of both services | `sudo systemctl status trader trader-dashboard --no-pager` |
| **Halt trading without stopping the process** | `trader-cli kill-switch engage` (or `sudo -u trader touch /opt/trader/data/STOP`) |
| **Resume after halt** | `trader-cli kill-switch release` (or `sudo rm /opt/trader/data/STOP`) |
| Switch strategy + flatten + restart | `trader-cli switch-strategy --name btc_sma --flatten --restart` |
| Check what's deployed | `grep -A2 '^strategy:' /opt/trader/config.yaml` |
| Inspect SQLite | `sqlite3 /opt/trader/data/trader.db 'select * from orders order by id desc limit 10;'` |

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

# Composer-equivalent YYPT comparison:
python -m trader backtest --strategy yypt_tqqq_rsi --start 2024-01-24 --end 2026-05-01 --cash 10000 --slippage-bps 1 --no-risk
```

**On the droplet:**
```bash
cd /opt/trader
sudo -u trader .venv/bin/python -m trader backtest --strategy yypt_tqqq_rsi --start 2018-01-01
# Reports go to /opt/trader/data/backtests/ and appear in the dashboard's Backtests tab.
```

Useful flags: `--strategy sma_crossover`, `--strategy yypt_tqqq_rsi`, `--no-risk`, `--cash 50000`, `--slippage-bps 10`, `--commission 0.50`, `--lookback-days 3650`.

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

Dashboard settings live in `/opt/trader/dashboard.env`, separate from
`/opt/trader/.env`. This keeps Alpaca trading keys out of the dashboard process.
To enable deploy/risk controls:

```bash
sudo cp /opt/trader/deploy/dashboard.env.example /opt/trader/dashboard.env
sudo nano /opt/trader/dashboard.env       # set DASHBOARD_PASSWORD and DASHBOARD_READ_ONLY=false
sudo chown trader:trader /opt/trader/dashboard.env
sudo chmod 600 /opt/trader/dashboard.env
sudo systemctl restart trader-dashboard
```

Leave `DASHBOARD_ENABLE_BROKER_READS=false` unless you deliberately want the
dashboard to hold Alpaca keys. In the secure default, dashboard deploys can
rewrite config and restart `trader`, but dashboard-side flattening is disabled.

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

## Troubleshooting

**"Insufficient buying power" rejections on BUY orders.**
The risk manager caps order qty to fit available BP with a 0.5% slippage
buffer. If you still see broker rejections, widen the buffer in
`risk.py::cap_qty_to_buying_power` — volatile crypto minutes can move >1%
between bar close and order submission.

**Deploy button (or `switch-strategy`) fails with `[Errno 30] Read-only file system`.**
The dashboard service's systemd sandbox doesn't include `/opt/trader/config.yaml`
in `ReadWritePaths`. Run `sudo bash /opt/trader/deploy/update.sh` to install
the corrected unit, or manually:

```bash
sudo install -m 644 /opt/trader/deploy/trader-dashboard.service \
  /etc/systemd/system/trader-dashboard.service
sudo systemctl daemon-reload && sudo systemctl restart trader-dashboard
```

**`switch-strategy --restart` exits with `rc=4`.**
The sudoers grant isn't installed or isn't being picked up. Verify:

```bash
sudo -u trader sudo -n systemctl is-active trader   # should print "active"
ls -la /etc/sudoers.d/trader-restart                # must be -r--r-----
```

If wrong, re-install: `sudo install -m 440 /opt/trader/deploy/sudoers.d/trader-restart /etc/sudoers.d/`
then `sudo visudo -cf /etc/sudoers.d/trader-restart` to validate.

**Bot keeps generating signals but never trades.**
Check `trader-cli kill-switch status` — `switch-strategy` leaves it engaged
on purpose. Release with `trader-cli kill-switch release`.

---

## What this still does NOT do

- Real-time data via websockets. Daily/15-min polling is fine for swing strategies.
- Options, futures, multi-leg orders.
- Multi-strategy capital allocation.
- Portfolio vol targeting, correlation-aware sizing, or drawdown management
  beyond the hard daily cap.

See `ARCHITECTURE.md` for the V3 design that addresses some of these.

---

## License

Personal use. Don't run someone else's trading code with real money without reading every line.
