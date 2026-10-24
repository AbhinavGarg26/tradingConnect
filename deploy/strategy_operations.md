> Page-based controls are now available after [one-time server setup](strategy_control_setup.md).
> The manual commands below remain available to the server operator.

# Strategy operations: PE1-V1 and Fixed Profit · Filtered

## What changed

`nifty_30m_reference_live.py --preset pe1-v1` retains the frozen first-PE rules,
existing database trade reservations, protected execution and guardian checks.
Its command now accepts `--user-id`; a PostgreSQL process lock additionally
prevents cross-host workers racing the same strategy/account. The separate v3
preset is unchanged. Do not run old v3 and new PE1 timers together.

The Fixed Profit production command now uses a database-first journal for runs,
entry/exit reservations, broker IDs, fills and premium P&L. Local journal tools
remain available to tests; production refuses to abandon unresolved local files.
Both runners default to paper and fail closed on prior session/trade state.
They do not automatically restore an intraday engine and replay signals after a
crash. Unknown submissions, partial fills and reservations require broker/DB
reconciliation; deleting a row/file to restart can create duplicate trades.
There is no blanket exactly-once guarantee across broker and database.

The performance page now shows five strategies' operations: runner heartbeat,
configured mode, last-run mode, timer active/enabled, next start and observation
time. This is reporting, not a dashboard activation toggle. A read-only systemd
observer writes status into strategy_service_observations. Rails never executes
systemctl. Missing or >3-minute-old observations show Unknown; running rows with
>2-minute-old heartbeats show Heartbeat stale. Mode comes from the effective
service command and must match the expected script, preset and account.

## Deployment

Deploy both projects, then apply the Rails migrations including
20261024040000_create_strategy_service_observations.rb (and the previous EMA
migrations if pending):

```sh
RAILS_ENV=production bundle exec rails db:migrate
```

In `/home/deployer/apps/tradingConnect`, review service paths and the Linux user.
Copy deploy/ema-strategies.env.example to deploy/ema-strategies.env if needed;
set EMA_USER_ID=975447485. Keep broker/database credentials in the existing
application environment, not in journal rows.

Read-only restart audit and runner checks:

```sh
.venv/bin/python strategy_restart_check.py --user-id 975447485 --mode live
.venv/bin/python nifty_30m_reference_live.py --user-id 975447485 --preset pe1-v1 --not-before YYYY-MM-DD --check
.venv/bin/python fixed_profit_filtered_live.py --user-id 975447485 --check
```

Replace YYYY-MM-DD with the intended trading date. Checks use broker reads;
quotes outside market hours may be stale. Neither command starts a session.
PE1 live entries additionally require saved live-approved risk settings,
sufficient allocation and a healthy strategy guardian as documented in
nifty_30m_reference_live.md. A live service command alone does not satisfy these.
Fixed Profit retains its frozen 15-point index stop / 30-point index target;
these require the process and fresh quotes, and are not exchange-native stops.

## Install paper services and status reporting

These commands are for the server operator to run; no units have been installed
or enabled as part of the code change.

```sh
sudo cp deploy/systemd/nifty-30m-pe1.service deploy/systemd/nifty-30m-pe1.timer deploy/systemd/fixed-profit-filtered.service deploy/systemd/fixed-profit-filtered.timer deploy/systemd/strategy-status-observer.service deploy/systemd/strategy-status-observer.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now strategy-status-observer.timer
sudo systemctl start strategy-status-observer.service
```

The observer only reads unit status and writes observations; it never starts or
stops trading units. It polls every minute, including outside market hours.
You can inspect without a database write using:

```sh
.venv/bin/python strategy_service_status.py --user-id 975447485 --print-only
```

Once ready for paper scheduling, enable the two paper-default timers yourself:

```sh
sudo systemctl enable --now nifty-30m-pe1.timer fixed-profit-filtered.timer
```

Both start weekdays 09:15:05 IST. Exchange-calendar checks still gate execution.
Startup must be before 09:45; enabling a timer later schedules the next session.
Persistent=false prevents stale catch-up on boot. Restart=no prevents automatic
order retry loops. If an old nifty-30m-reference-v3 timer is enabled, explicitly
stop/disable that timer first after inspecting any running service/position;
stopping a timer does not stop its already running service or close positions.

## User-controlled live mode

After reviewing paper results and resolving existing broker/DB state, the user
can edit each unit with `sudo systemctl edit NAME.service`. Reset ExecStart and
supply the full command with --live:

```ini
[Service]
ExecStart=
ExecStart=/home/deployer/apps/tradingConnect/.venv/bin/python /home/deployer/apps/tradingConnect/nifty_30m_reference_live.py --user-id 975447485 --preset pe1-v1 --live
```

For fixed-profit-filtered.service use:

```ini
[Service]
ExecStart=
ExecStart=/home/deployer/apps/tradingConnect/.venv/bin/python /home/deployer/apps/tradingConnect/fixed_profit_filtered_live.py --user-id 975447485 --live
```

Review with `sudo systemctl cat NAME.service`, then daemon-reload. Existing
enabled timers use the new command at the next start. Do not change mode on a
running service. Paper trades never become live positions. No profitability or
option-return validation is implied by a service running successfully.

## Monitoring and restart

Use the Strategy operations table, recent runs, open/unresolved trades and events.
Logs:

```sh
sudo journalctl -u nifty-30m-pe1.service -u fixed-profit-filtered.service -u strategy-status-observer.service -f
```

After a crash, compare broker orders/fills/positions with persisted reservations.
PE1 uses its existing reference journal/protection/guardian reconciliation.
Fixed Profit records pending reservations before order submission and blocks a
second session attempt. Reconcile status/ownership before any operator restart;
no automatic state reset is supplied. Older local journals are not imported as
assumed fills; reconcile them separately. A paper or live label is a mode, not
proof of a broker fill. Stopping timers/services does not liquidate holdings.
