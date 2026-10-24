> Earlier interpretation, retained as historical research. The clarified price-versus-single-EMA request is implemented in `nifty_price_ema_timeframes.md`.

# Fixed 12/36 bearish crossover across timeframes

Interpretation of the requested experiment: EMA12 crosses below EMA36 to enter;
EMA12 crosses above EMA36 to exit. It is not an intraday quote-driven signal.
Only completed candles create decisions, filled at the following candle open.
Stay in cash otherwise and allow overnight holds. No stop or target is added.

The selected 15m 9/21 research benchmark is frozen in
nifty_bearish_15m_9_21_frozen.json, including rules, original metrics, excluded
source dates and SHA-256 checksums for the original input and trades. The
comparison verifies those inputs have not changed; no baseline retuning or
trading activation occurs.

```sh
venv/bin/python -m backtest.nifty_bearish_ema_timeframes --fast 12 --slow 36
```

The runner reuses previously verified database exports for 15m, 30m, 1h and
daily candles. Each EMA is warmed up on its 2020 history, then evaluated from
2021-01-01 through 2026-10-23. Accounts start flat and wait for a fresh bearish
cross. All fills use 0.5 adverse Nifty points. Source hashes and coverage
references are retained in outputs/nifty_bearish_12_36_timeframes/comparison.json.
Per-timeframe trades, signals, point curves and readable comparison.md are saved.

All entry signals are audited: previous fast >= slow, current fast < slow.
Source minute exclusions, final shortened session bars and final open positions
are handled consistently with earlier comparisons. Historical data has already
been inspected; this is research, not independent validation.

Results are underlying directional points. Monthly put premiums, rollover,
lot sizes, premium budgets and option costs cannot be tested from this data.
No option P&L, actual account return or new live/paper strategy is inferred.
