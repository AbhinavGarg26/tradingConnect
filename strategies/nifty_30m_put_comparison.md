# 30-minute bearish-only EMA research

Separate research from the calls-only monthly daily strategy. No broker orders,
paper trades, performance rows or strategy settings are changed by this test.

Bearish entry is audited explicitly on every completed candle:
previous fast >= previous slow AND current fast < current slow. Exit is previous
fast <= previous slow AND current fast > current slow. No entries are made just
because fast was already below slow when evaluation started. Fill at the NEXT
observed candle open, with 0.5 adverse Nifty points per fill. In a bearish index
proxy, entry is open minus slippage and exit is open plus slippage.

Source: existing market_snapshots NIFTY 50 / 1m. Each complete NSE session is
aggregated from 09:15 IST into twelve 30-minute candles and the final shortened
15:15–15:30 session candle. That final candle participates in EMA calculation;
its signals execute at the following session's open. Bars do not cross days.
Missing/invalid minute data stops the runner unless whole affected days are
explicitly excluded; 13 dates were excluded in this run and listed in JSON.

All five pairs use the same 2020 indicator warmup and evaluation 2021-01-01
through 2026-10-23. EMAs start from the first source close and use alpha=2/(N+1).
Positions carry overnight. There are no protective stops, targets, additional
trend filters or option expiry exits. Final open positions are marked separately
and excluded from closed-trade win rates. Drawdown uses cumulative marked index
points from the initial zero, not account equity or a hypothetical rupee budget.

```sh
venv/bin/python -m backtest.nifty_30m_put_comparison --database --skip-incomplete-days
```

Outputs are in outputs/nifty_30m_put_comparison: comparison.md, comparison.json,
aggregated source candles, and per-pair trade/signal/point-equity files. The
summary records entry_rule_violations; all five have zero. Historical prices
and the final partial session candle are available for independent inspection.
Six unit tests cover bearish direction, next-bar fills, no future leakage,
source completeness, timezone/session aggregation, overnight opening fills,
and pending final signals.

Results: 9/21: 354 closed trades, 35.0% wins, +1704.15 points, PF 1.06.
10/30: 279 trades, 34.8% wins, +3202.50 points, PF 1.14. The remaining three pairs
had negative closed-trade index points. These comparisons do not independently
validate the best historical pair.

This is NOT a historical put-premium backtest. Missing option histories prevent
modeling decay, volatility, historical lot sizes, monthly rollover, premium
budgets and option charges. A profitable index proxy need not be profitable in
puts. No option return or rupee profit is inferred.
