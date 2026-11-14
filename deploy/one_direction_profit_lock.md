# One Direction Profit Lock V2.1 service

The installed service is paper-only. It uses completed database-backed NIFTY
15-minute candles and executable option bid/ask quotes, selects the nearest
listed unexpired NIFTY weekly expiry, and chooses the nearest ATM CE or PE.
It journals fills, profit-lock changes, reversals and restart state durably.

A protected-live code path exists but fails closed unless all of these pass:

- an accepted historical option bid/ask backtest after configurable spread,
  slippage, brokerage, STT, exchange, SEBI, stamp and GST costs;
- at least 30 genuine closed paper-option trades over 10 sessions, including
  five profit-lock events and two reversal exits;
- enabled allocation, per-trade risk and daily-loss settings;
- a fresh independent guardian heartbeat and broker stop-limit protection.

Index-proxy day-end rows never count as forward evidence. Missing historical
option quotes keep live mode blocked rather than substituting NIFTY returns.

Validate broker access and the selected contracts without starting a session:

```sh
.venv/bin/python one_direction_profit_lock_service.py --user-id USER_ID --check
```

Install the paper timer only after the check succeeds:

```sh
sudo cp deploy/systemd/one-direction-profit-lock.service deploy/systemd/one-direction-profit-lock.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now one-direction-profit-lock.timer
```

The checked-in unit matches the production host layout at
`/home/ubuntu/tradingConnect` and runs under the unprivileged `ubuntu` user.

`Persistent=false` prevents late catch-up. The service must accumulate paper
stop-fill evidence before protected live mode can pass its evidence gate.

Generate the current readiness report:

```sh
.venv/bin/python one_direction_profit_lock_readiness.py --user-id USER_ID
```

The historical premium tool requires a CSV containing genuine option
`entry_ask` and `exit_bid` values plus contract and time fields. Every charge
rate is a required command-line input so the report cannot silently use stale
fee assumptions:

```sh
.venv/bin/python -m backtest.one_direction_option_premium OPTION_QUOTES.csv \
  --output outputs/one-direction-option-backtest.json \
  --brokerage-per-order RATE --stt-sell-pct RATE --exchange-pct RATE \
  --sebi-pct RATE --stamp-buy-pct RATE --gst-pct RATE \
  --slippage-ticks TICKS --tick-size TICK
```
