> Page-based controls are now available after [one-time server setup](strategy_control_setup.md).
> The manual commands below remain available to the server operator.

# Deploying the two EMA option runners

The shared `ema_strategy_runner.py` implements daily EMA20/50 monthly calls
(`daily_calls`) and frozen 15-minute EMA9/21 monthly puts (`bearish_puts`).
They are separate from Nifty 15m Breakout v3. Service templates default to paper.
No deployment, migration, timer activation or broker order was performed while
preparing these changes.

## Deploy and check

Deploy both the Python project and the portfolio-tracker Rails changes. In the
Rails production directory, apply migrations using the normal deployment process:

```sh
RAILS_ENV=production bundle exec rails db:migrate
```

Required migrations include `20261024020000` (state and order journal),
`20261024030000` (atomic fill-application marker), and nullable stop/target columns.
From `/home/deployer/apps/tradingConnect`, using your server virtual environment:

```sh
.venv/bin/python strategy_restart_check.py --user-id 975447485
.venv/bin/python ema_strategy_runner.py --strategy daily_calls --user-id 975447485 --mode paper --check --skip-incomplete-days
.venv/bin/python ema_strategy_runner.py --strategy bearish_puts --user-id 975447485 --mode paper --check --skip-incomplete-days
```

`--check` is read-only, including with `--mode live`. It checks session/data/schema
and reports state; it does not certify profitability or enable orders. Broker
login must be valid for the trading day. Historical one-minute Nifty data comes
from market_snapshots, current intraday candles and option quotes from Kite.
Missing old sessions can be explicitly excluded; missing latest-session data,
current-session gaps and stale quotes block execution.

## Start with paper services

Review the service paths, Linux user and virtualenv for your server. Copy
`deploy/ema-strategies.env.example` to `deploy/ema-strategies.env`, retaining your
user ID. Use the normal environment loader for database and broker configuration;
do not put credentials in strategy state. Install the four supplied unit files:

```sh
sudo cp deploy/systemd/nifty-daily-ema-calls.service deploy/systemd/nifty-daily-ema-calls.timer deploy/systemd/nifty-bearish-15m-ema.service deploy/systemd/nifty-bearish-15m-ema.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nifty-daily-ema-calls.timer nifty-bearish-15m-ema.timer
sudo systemctl list-timers 'nifty-*'
```

These commands are for you to run on the server. The shipped commands use
`--mode paper`: they never submit broker orders. Timers start at 09:14 IST on
weekdays; runners additionally check the exchange calendar, wait until 09:15,
and stop at 15:30 without squaring off overnight positions. Persistent=false
prevents boot-time catch-up. If you enable a timer after its start time, the next
scheduled session is used. A mid-session paper trial can be started manually
with `sudo systemctl start NAME.service`.

## User-controlled real-order activation

First inspect your broker's positions and orders, reconcile any unresolved state,
and review paper results. Historical index tests do not establish option returns.
Each strategy can hold one lot with at most ₹30,000 premium per entry; both can
be open concurrently. There is no fixed stop or target; the whole premium can
be lost, and subsequent rollovers can incur further losses.

For real orders, you must change each service explicitly. For calls, run
`sudo systemctl edit nifty-daily-ema-calls.service` and save:

```ini
[Service]
ExecStart=
ExecStart=/home/deployer/apps/tradingConnect/.venv/bin/python /home/deployer/apps/tradingConnect/ema_strategy_runner.py --strategy daily_calls --user-id 975447485 --mode live --acknowledge-premium-risk --skip-incomplete-days
```

For puts, edit `nifty-bearish-15m-ema.service` with the same override but use
`--strategy bearish_puts`. Review each effective command with
`sudo systemctl cat NAME.service`, then run `sudo systemctl daemon-reload`.
An already enabled timer will use the new command at its next start. If it is
not enabled, enable the two timers using the command above. Do not switch a
running service's mode: stop it first and reconcile its account state.
Paper positions do not become live positions; live mode has a separate journal.
The dashboard displays the latest run mode/status; its enable switch does not
control these standalone services.

Entries require a fresh completed crossover: daily signals are eligible at the
next session opening until 09:30; 15-minute signals have a 90-second window,
with a final-session-bar signal eligible next opening. Starting while the EMA
trend is already active does not buy retrospectively. Exits of owned positions
can catch up after downtime. Monthly rollovers close the owned lot before
opening the replacement, two trading sessions before expiry.

## Restart behaviour and monitoring

State is stored in strategy_execution_states; immutable order reservations and
broker IDs in strategy_order_intents; fills/positions in strategy_trades; signals
and errors in strategy_events; heartbeat/mode in strategy_runs. Each reservation
is committed before submission. A database lock prevents concurrent workers;
unique signal/action keys survive restarts. Confirmed fills, owned position,
checkpoint and applied_at marker commit together. A restart never assumes that
a network timeout means the broker rejected the order.

Unknown, partially filled, conflicting or changed broker positions halt for
reconciliation. Rejected exits also halt. Do not delete state or change intent
keys to retry. A BUY uses a capped limit price; an unfilled/resting order may
need manual broker review. There is no automatic cancellation or blind resend,
and no absolute exactly-once guarantee across broker and database.

```sh
sudo journalctl -u nifty-daily-ema-calls.service -u nifty-bearish-15m-ema.service -f
```

After a failure, inspect the journal, read-only restart check and broker account.
Resolve any resting order/partial fill and reconcile stored ownership before
restarting. Services use Restart=no to avoid automatic retry loops. Do not clear
a halted checkpoint without understanding and correcting the cause. Stopping a
service/timer prevents future automation but does not close broker positions.
