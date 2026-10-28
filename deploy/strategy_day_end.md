# Strategy day-end paper backfill

`strategy_day_end.py` runs after market close and examines the latest ten NIFTY
trading sessions. It only replays strategies whose service is running or whose
timer is enabled in `strategy_service_observations`.

An existing trade with the same strategy, side and entry signal time is never
changed, regardless of whether it is live or paper. A missing closed signal is
inserted as a paper index-proxy trade. The performance page labels these rows
`paper · day-end replay` and does not claim an option-premium or rupee return.

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

The job reads historical candles and instruments through the existing Kite
session. It never calls an order endpoint. Failed runs are retried by the next
scheduled invocation; inserts are deduplicated against both live and paper
trades.
