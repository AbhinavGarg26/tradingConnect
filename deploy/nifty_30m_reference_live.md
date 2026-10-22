# Frozen 30m V3 live deployment

Production service: `nifty-30m-reference-v3.service`.
Timer: `nifty-30m-reference-v3.timer`, weekdays at09:15 Asia/Kolkata.
Live eligibility starts October23,2026. Earlier invocations return without
orders. Calendar checks skip holidays/weekends. The October22 timer may wake
and safely return; October23 is the first eligible live date.

## Exact live configuration

- Core `Nifty30mReference(stop_points=25,max_entries=3,buffer_percent=5)`.
- First30m body>=20; no high/low exception. Matching-color >40/80 reference
  updates and original reference exits. No EMA, doji, color, trailing, or
  reference-exit-buffer experiments.
- One exchange lot from the daily NFO instrument list. Buy nearest unexpired
  NIFTY expiry, CE two listed strikes above nearest ATM, PE two below.
- At entry, choose using fresh Nifty quote. An occupied strike is blocked
  across CE/PE and all listed unexpired NIFTY expiries. Pending orders also
  block strikes. If occupied, search successively lower numeric strikes of
  the same option side and selected expiry. Do not merge into a manual lot.
- Three entries total per day, combining live and paper fallback.
- Stop remains25 Nifty points from the index quote sampled after the buy fill.
  Exits are completed-minute close triggers and can fill beyond that level.
  Reference levels and calendar cutoff are also index based, not option premiums.
- Position retains its chosen option until exit; do not chase ATM while holding.

## Funds fallback

Only an API MarginException or an order confirmed REJECTED for insufficient
funds with zero filled/pending quantity can cause paper fallback. Recheck no
live position or pending order exists on that contract before simulating.
No fallback on timeout, cancellation, partial fill, uncertain submission or
exit failure. Those states require reconciliation. Do not submit duplicate
orders after an uncertain response. Never simulate closing an actual live lot.

The fallback trade uses current quote prices and mode=paper in the same daily
run, with a recorded insufficient-funds reason. Simulated premium P&L is not
real broker P&L. The next eligible entry tries live execution again; fallback
is per trade, not a permanent daily switch. Entry count still applies.

## State and monitoring

PostgreSQL journal uses strategy key `nifty_30m_reference`, version `v3`, and
existing strategy_runs/trades/events. Target is NULL because V3 has no target;
SQL setup only removes target's NOT NULL constraint. Existing strategies retain
numeric targets. Rails migration20261022030000 tracks this change for future
Rails deployments. The `/strategy_performance` page separates live/paper modes.
Runtime health key is `nifty_30m_reference_v3`.

The runner starts09:15–09:45 only, retries unavailable Kite sessions every five
minutes until09:45, and never late-starts missed signals after the range closes.
Refresh the broker session using the usual app login before the trading day.
Current-token readiness does not make tomorrow's login/session automatically valid.

Completed one-minute candles are polled every three seconds with a two-second
completion grace. The first eligible V3 signal is09:46. Exits/stop monitoring
require the process and broker connection to keep working; these index stops
are not broker-native option stop orders. A wall-clock15:15 session-end exit
also operates if historical data is temporarily delayed and fresh quotes work.

The service does not automatically restart while it might own a live option.
Any prior V3 trade today or unresolved prior position blocks startup. A failure
with an owned/pending trade marks reconciliation and surfaces through the usual
runtime monitor. Never restart or stop it while open without reconciling the
broker account. Manual changes to its contract quantity/product or pending
orders block automatic selling, preventing sale of another/manual lot.

## Operations

Read-only readiness, no orders:

```sh
.venv/bin/python nifty_30m_reference_live.py --check --not-before 2026-10-23
```

Daily service command, already installed in the systemd unit:

```sh
.venv/bin/python nifty_30m_reference_live.py --live --not-before 2026-10-23
```

Without --live the runner is paper-only, but still requires market data and
journal availability. Inspect runtime:

```sh
systemctl status nifty-30m-reference-v3.timer nifty-30m-reference-v3.service
journalctl -u nifty-30m-reference-v3.service -f
```

The enabled timer is linked to units under
`/home/deployer/apps/tradingConnect/deploy/systemd/`. No existing live service
was restarted or disabled by this deployment. Disable only this timer to
prevent future scheduled starts; an already-running service continues managing
its current session.

## Deployment validation (October22)

-124 existing strategy tests and18 live-execution tests passed locally.
-The18 live-execution tests also passed on production.
-Production no-order readiness passed; October23 is a trading day and the
 nearest eligible listed expiry is October27.
-Production NULL-target journal insert/event test passed inside a transaction
 rolled back in full. No smoke trades/runs persisted.
-Timer enabled/active. Verified live invocation on October22 returns at the
 not-before gate. No real order was placed during setup.
