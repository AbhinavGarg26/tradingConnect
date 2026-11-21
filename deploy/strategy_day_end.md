# Strategy day-end paper backfill

`strategy_day_end.py` runs after market close and examines the latest ten NIFTY
trading sessions. It replays strategies whose service is running or whose timer
is enabled in `strategy_service_observations`. It also always includes the
paper/index replay for `one_direction_profit_lock`, which cannot place orders.
The equity replays, including `gap_up_volume_confirmed`, read only cached daily
and 15-minute `market_snapshots` and never submit broker orders.

The Profit Lock replay reconstructs direction and reversal signals from NIFTY
candles, then retrieves the selected ATM option's one-minute Kite candles. It
uses an adverse OHLC fill model (two ticks per side), applies the initial 10%
option-premium loss stop, and activates the +10% option-premium profit lock
from the option's actual high. If the historical option contract or candles are
unavailable, it skips the trade rather than creating an index proxy.

An existing live trade is never changed. A prior Profit Lock index proxy with
the same signal is upgraded only when the exact option candles can be obtained;
normal paper-forward trades are never overwritten.

Test the report without writing first:

```sh
.venv/bin/python strategy_day_end.py --user-id "$EMA_USER_ID" --dry-run
```

Install and enable the supplied timer on the trading server after reviewing its
paths and account:

```sh
sudo install -o root -g root -m 0644 deploy/systemd/strategy-day-end.service /etc/systemd/system/
sudo install -o root -g root -m 0644 deploy/systemd/strategy-day-end.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now strategy-day-end.timer
```

Install the dedicated gap-up paper schedule when enabling that strategy:

```sh
sudo install -o root -g root -m 0644 deploy/systemd/gap-up-volume-confirmed-paper.service /etc/systemd/system/
sudo install -o root -g root -m 0644 deploy/systemd/gap-up-volume-confirmed-paper.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gap-up-volume-confirmed-paper.timer
```

The job reads historical candles and instruments through the existing Kite
session. It never calls an order endpoint. Failed runs are retried by the next
scheduled invocation; inserts are deduplicated against both live and paper
trades.
