# NIFTY noise-area live integration

The runner is live-capable but defaults to paper. No service is installed,
enabled, scheduled, or started by this repository change.

## Readiness

Deploy both repositories and apply existing Rails migrations. The database must
contain at least fourteen complete prior NIFTY one-minute sessions. Then run:

```sh
.venv/bin/python nifty_noise_area_live.py --user-id 975447485 --check
```

The check authenticates the broker, validates historical coverage and prints
`orders: DISABLED`. It never creates a run or submits an order.

## Install in paper mode

```sh
sudo cp deploy/systemd/nifty-noise-area.service deploy/systemd/nifty-noise-area.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nifty-noise-area.timer
```

The timer starts at 09:15:05 IST on weekdays; the exchange calendar still
blocks holidays. `Persistent=false` prevents a late catch-up start. A session
must start before 09:40 because replaying missed live decisions is unsafe.

## Live prerequisites

Before selecting live mode in Manage strategy:

1. Review forward paper trades and reconcile every open/pending row.
2. Save capital allocation, per-trade risk and maximum daily loss for
   `nifty_noise_area` in Strategy risk.
3. Keep broker stop-limit plus emergency exit selected and enable live entries.
4. Install and verify the independent strategy guardian.
5. Confirm the effective service command and account ID.

The runner buys one current-expiry ATM CE or PE. If one lot plus the allocation
cushion does not fit, the entry is rejected; exchange lots cannot be reduced
below one lot. A confirmed option fill must receive a broker-held stop-limit.
If protection fails, the runner attempts an immediate market exit. Ambiguous
orders, partial fills, position differences or unconfirmed cancellation halt
the session for manual reconciliation.

Use the dashboard control only after these prerequisites. Stopping or pausing a
timer does not close an existing broker position. This integration does not
convert the index backtest into verified option profitability.
