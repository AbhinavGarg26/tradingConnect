# Fixed Profit filtered candidate — user-operated runner

Status on 24 October 2026: implementation and offline verification complete;
**no broker-connected trading session started, no orders submitted, and no live
service/timer enabled**. The read-only readiness check failed because the Kite
session token was expired or missing. Refresh it through the existing Rails UI.
The assistant has not activated real-money trading; the commands below are for
the user to run manually.

## Frozen strategy

Strategy key `fixed_profit_filtered`, version `v1-fixed-entries`.
Settings and fingerprint are defined in `strategies/fixed_profit_filtered.py`;
each durable session journal records their exact values and SHA-256 hash.
There are no CLI overrides for trading parameters.

- Opening reference: completed 09:15–09:45 IST high/low; range strictly >40.
- Entry: existing separate breakout/retest on completed 5m candles. A breakout
  closes >2 points beyond the reference; a later directional retest must touch
  within 2 points within 30 minutes. Original 10-point structural qualification
  stays unchanged, including a stop at least 1 point beyond the retest wick and
  a maximum 10-point distance from the reference.
- Filter: 14 completed session 5m bars, simple ATR14 ≤20; reference close crossings
  since 09:45 ≤2. Earliest ATR-qualified entry is 10:25. A filter-rejected original
  eligible entry skips the entire day; no later replacement entry is searched.
- One confirmed trade per session. No new entry at/after 14:30. Square off 15:15.
- Actual risk: 15 Nifty index points stop / 30 index points target, no trailing.
- No EMA gate is added. The tested 36/100 gate did not establish an improvement.
- Option: nearest unexpired listed NIFTY expiry, nearest ATM strike (lower on
  ties), one exchange lot using the contract's current lot size. Buy CE upward,
  PE downward. Never change the owned contract during a trade.
- Skip occupied ATM contracts or contracts with pending orders; do not merge
  with another/manual strategy position or silently choose a different strike.

The 89 historical dates are not a preselected future trading calendar. They are
the historical entries that pass these rules. Future entries depend on future
data and valid fills, so the live count cannot be fixed to 89.

## Offline verification

The same `FilteredSession` controller used by the runner reproduced every one
of the saved 89 entry dates, sides, times, fills and 15-stop outcomes. Summary:
42.70% wins, +375 gross index points, +197 after an illustrative 2-point deduction
per trade, gross max drawdown 120 points. This is not actual option P&L.

```bash
venv/bin/python -m backtest.fixed_profit_filtered outputs/fixed-profit-36-100/nifty_minute_source.csv
```

Outputs: `outputs/fixed-profit-filtered-runner/analysis.json` and `trades.csv`.
The prior validation limitations still apply: 2024–2025 after-cost result was
−13 points and later samples are small. The code matches the research candidate;
this does not prove future profitability.

## Readiness and manual startup

Run from the project directory. Read-only readiness authenticates Kite, checks
the local trading calendar/current options and existing strategy orders; it does
not create a session or place orders:

```bash
venv/bin/python fixed_profit_filtered_live.py --check
```

Start manually on an exchange trading day between 09:15 and 09:45 IST. Holiday
and weekend checks apply. Default is **paper**, using real quotes and simulated
premium fills; it never calls the order endpoint:

```bash
venv/bin/python fixed_profit_filtered_live.py
```

For user-operated real-order mode, after refreshing the token and reviewing the
fixed settings, the user can explicitly run:

```bash
venv/bin/python fixed_profit_filtered_live.py --live
```

No background job, service, or timer has been installed or enabled. This runner
is separate from existing 15m/30m strategies and does not stop or modify them.

## Execution and monitoring limits

Entry candles are fetched as completed minute data with a two-second grace and
aggregated to 5m. Original entry qualification is rechecked against a fresh
index quote before submission. Entry signals older than 15 seconds are skipped
for the entire day; delayed history never triggers catch-up trades.

Risk is anchored to the fresh index sample immediately before the option order,
which is recorded as `pre_order_index_quote`. The broker fills the option premium;
there is no simultaneously guaranteed index fill. A moved quote after order fill
must never cause the program to reject or forget an already-owned option.

Stop/target monitoring uses fresh index quote polling (nominally every 3 seconds)
and a 15:15 wall-clock exit before historical requests. These are process-managed
index triggers, **not broker-native option stop orders**. Quote gaps, market/API
outages, slow requests, process death and slippage can cause losses beyond 15
points or missed transient targets. Actual fills cannot match an OHLC backtest
exactly. Option premium returns differ from Nifty index points.

The dedicated broker tag is `FIXPROFITFILTER1`. The adapter submits an order once,
then waits for COMPLETE and the exact quantity/positive fill price. Timeouts,
partial fills, cancelled/rejected orders and missing history stop the session
for reconciliation; there is no automatic retry or funds-to-paper fallback.
Kite market protection reduces order-price extremes but does not guarantee a
fill or implement the index stop: [official order documentation](https://kite.trade/docs/connect/v3/orders/).

Before selling, live mode verifies the exact owned quantity and MIS product and
absence of pending orders on the contract. A manual change blocks automatic
selling rather than risk closing someone else's lot.

## Journals and restart behavior

`outputs/fixed-profit-runtime/` stores atomic per-user/date/mode JSON files and
an exclusive account lock. File permissions start at 0600. The journal records
entry and order intents before submission, confirmed fills, fixed risk, exit
intents, heartbeat and errors. It is a local journal, not yet integrated into
the Rails strategy-performance tables.

The same-day journal prevents automatic restart/re-entry. Any prior unresolved
live entry prevents a later live start. An existing order with this strategy's
tag today also blocks startup, even if completed. Never delete/move journal
files to bypass these guards while a broker order or position might exist.
On failure or uncertain submission, inspect broker orders, trades and positions
and reconcile manually. There is no automatic adoption or unattended restart.

## Market-open schedule template

Two inert templates are provided in `deploy/examples/`:
`fixed-profit-filtered.service.example` and
`fixed-profit-filtered.timer.example`. They have not been installed, enabled,
or started. They are outside the normal service directory and deployment scripts
have not been changed to activate them.

For a user choosing to configure real-order scheduling on the existing Linux
host, the timer template wakes at 09:15 Asia/Kolkata on weekdays. The runner
checks the exchange calendar and refuses a late start. `Persistent=false`
prevents a missed opening from becoming an automatic catch-up start. The service
has `Restart=no`; reconciliation and same-day journal guards remain in force.
Update the host/user/paths as needed, refresh Kite credentials before each
session, and review the process-managed stop limitations above before manually
installing or enabling these templates. The assistant does not activate them.

The app now lists `Fixed Profit · Filtered` under Strategy performance, Strategy
risk settings, and watchlist strategy choices. These are catalog entries, not
an activation switch. The standalone local journal is not a Rails trade feed;
the dashboard explicitly states that run/trade synchronization is not connected.
