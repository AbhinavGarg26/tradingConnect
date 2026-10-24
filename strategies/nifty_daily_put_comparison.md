# Separate bearish-only EMA research

This is isolated from the active calls-only monthly strategy. It does not enable
puts, change its settings, insert paper trades, or activate broker execution.

For each pair (9/21, 10/30, 20/50, 20/100, 50/200):
- On a completed downward crossover, enter a bearish Nifty index signal at the
  next session open. On upward crossover, exit at next session open to cash.
- Use identical 2020 warmup history, evaluation from 2021-01-01 to 2026-10-23,
  flat starting state, and 0.5 adverse index points of slippage per fill.
- Final open bearish legs remain open and marked separately. Closed-leg wins,
  profit factor and net points exclude those final unrealised results.

Run all five using the cached verified database export:

```sh
venv/bin/python -m backtest.nifty_daily_put_comparison
```

Inputs and source coverage are explicit CLI options (--csv, --source-report).
Reports and per-pair trades/signals/point curves are in
outputs/nifty_daily_put_comparison; comparison.md is the readable table and
comparison.json includes full summaries and exit-year closed-leg breakdowns.

These are not put option returns. Actual option premiums, expiry rollover,
historical lot sizes, the INR 30,000 one-lot cap, and option fees are not modeled
because historical option data is missing. A profitable underlying bearish leg
may still lose money in puts; negative aggregate index points likewise do not
prove the exact premium result. The 2021 onward sample excludes the 2020 crash
from evaluation because it is used for common warmup.

Observed closed-leg results: every tested pair had negative index points in
this period. No put variant is selected or promoted. The calls-only monthly
implementation remains unchanged.
