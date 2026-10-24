# CE timeframe/crossover research

Daily 20/50 calls-only remains the operational baseline. This separate study
mirrors the earlier PE index-signal comparison on identical sources and dates.
Test six EMA pairs plus price crossing above single EMA12 or EMA36 across 15m,
30m, 1h and daily candles. Enter upward crosses, exit downward crosses, stay in
cash otherwise, use next-open fills and 0.5 adverse index points per fill.

```bash
python3 -m backtest.nifty_bullish_ema_timeframes
```

Read `outputs/nifty_bullish_ema_timeframes/comparison.md` and its all-case CSV.
Results are Nifty signal points, not actual call-option returns or monthly-roll
performance. No premium data, expiry rolls, lot sizing, premium cap or option fees
are modeled. Prior warmup, common flat start, completed-candle rules and pending
final signals prevent retrospective fills. Existing calls and PE configurations
and trading services are unchanged.
