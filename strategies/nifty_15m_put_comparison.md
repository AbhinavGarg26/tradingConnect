# 15-minute bearish-only EMA research

Five fixed pairs: 9/21, 10/30, 20/50, 20/100 and 50/200. This research is separate
from the calls-only monthly strategy and changes no live or paper journal.

Source is market_snapshots NIFTY 50 / 1m, aggregated into 25 complete 15-minute
candles from 09:15 through 15:15 IST. No bucket crosses sessions. A regular
session requires all 375 minutes; incomplete dates are explicitly excluded and
listed in comparison.json. The 2020 history is indicator warmup, and the common
evaluation is 2021-01-01 through 2026-10-23. All pairs start flat.

Entry: previous fast EMA >= previous slow EMA, current fast EMA < current slow
EMA, observed only after the candle completes. Exit: the opposite crossover.
Fill at next candle open, including the following session's open for a signal
at 15:30. Stay in cash after exit. Hold overnight if no bullish crossover.
There is no stop, target, trailing exit or additional price/volume filter.
Every historical entry is audited against the exact bearish inequality.

The same engine as the 30m and 1h experiments is used, with 0.5 adverse Nifty
points per fill. Net points and win rate describe closed bearish index legs;
the last open leg is marked separately and excluded from closed-trade wins.
These are NOT actual put-option returns: historical premiums, expiry rolls,
lot sizes, option costs and the INR 30,000 cap remain untested.

```sh
venv/bin/python -m backtest.nifty_15m_put_comparison --database --skip-incomplete-days
```

Results, source candles, audited signals, trades and marked point curves are
saved to outputs/nifty_15m_put_comparison. Do not infer option rupee P&L from
Nifty points or select a live system solely from this historical comparison.
