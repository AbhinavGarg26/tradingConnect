# Price below EMA12 or EMA36 — corrected bearish research

This implements the clarified request: price crosses below a SINGLE EMA12 or,
in a separate experiment, a SINGLE EMA36. It is not the earlier assumed 12/36
moving-average crossover. The original 15m 9/21 benchmark remains frozen.

On each completed candle:
- Enter bearish signal if previous close >= previous EMA and current close <
  current EMA. A wick touching/crossing alone does not qualify.
- Exit if previous close <= previous EMA and current close > current EMA.
- Fill at the next candle/session open. Stay in cash after exit and wait for
  another downward cross. Do not enter just because price was already below
  the EMA when evaluation started. Hold overnight; no stop or target.

Test separately on 15m, 30m, 1h and daily candles. Prior 2020 history seeds each
EMA (alpha=2/(period+1)); common evaluation 2021-01-01 to 2026-10-23. Use the same
source coverage and 13 excluded incomplete dates as earlier tests, with adverse
slippage of 0.5 index points per fill. Final open legs are marked separately,
not classified as closed-trade wins.

```sh
venv/bin/python -m backtest.nifty_price_ema_timeframes
```

The audited engine uses EMA1 as the price identity: alpha=1 makes its value
exactly the candle close, with no smoothing. This is a code reuse mechanism,
not a second trading indicator. Regression tests verify both single-EMA periods
against direct close/EMA inequalities and known next-open fills. Every historical
entry is also audited for the intended current/previous relationship.

Results and per-case trade files are under outputs/nifty_price_ema_timeframes.
Read comparison.md for all eight cases, or comparison.json for source hashes,
full detail, final open legs and the frozen baseline reference. None of these
experiments establishes high win probability. These are index signals, not
actual option premiums, monthly roll returns, or rupee account performance.
Missing historical option data still prevents that option backtest. No trading
service, calls-only configuration or journal is changed.
