# Daily 20/50 two-sided crossover: directional signal backtest

This implements the signal sequence requested for monthly calls and puts:
completed bullish crossover -> bullish (CE-labelled) index leg; completed
bearish crossover -> close the bullish leg and start a bearish (PE-labelled)
index leg at the next observed session open. The inverse reversal closes the
bearish leg and opens the bullish leg. No same-close fills are assumed.

The stored database has Nifty index history but no Nifty option candle series in
market_snapshots. Its options table is not a dated historical price series.
Therefore this runner explicitly reports blocked_missing_historical_option_prices
for the ACTUAL monthly-option backtest. It cannot evaluate the monthly contract
rolls, one-lot premium affordability, 30,000 rupee cap, decay, implied volatility,
bid/ask execution, option costs or rupee P&L. Directional signal wins must not be
presented as profitable call/put option trades.

## Run and outputs

```sh
venv/bin/python -m backtest.nifty_daily_ema_switch --database --from-date 2020-01-01 --to-date 2026-10-23 --evaluation-start 2021-01-01 --skip-incomplete-days
```

Or reuse the prior database export:

```sh
venv/bin/python -m backtest.nifty_daily_ema_switch --csv outputs/nifty_daily_ema_comparison/daily_input.csv
```

Source: 2020 onward for EMA warmup; common evaluation from 2021-01-01 through
2026-10-23. Thirteen incomplete source sessions were excluded in the prior
export. The latest daily candle must be complete in supplied input. The runner
starts flat and waits for a fresh crossover; initial existing trends do not
create retrospective entries. EMA seeds/formula match the earlier daily tests.
Each directional fill pays adverse slippage of 0.5 INDEX points. No option
transaction charges are modeled. The final open leg is marked separately,
never counted as a closed-trade win. Pending final crossovers await a next open.

Outputs: summary.json, trades.csv, signals.csv, equity_points.csv under
outputs/nifty_daily_ema_switch. No strategy journal is modified; no paper/live
trades are inserted. The CSV route has no automatic knowledge of previously
excluded source dates; consult the source export's comparison.json.

## Observed signal results

On 1,421 evaluation sessions:

| Direction | Closed legs | Wins | Win rate | Net underlying points |
| --- | ---: | ---: | ---: | ---: |
| Bullish / CE-labelled | 11 | 7 | 63.6% | +6,989.65 |
| Bearish / PE-labelled | 11 | 4 | 36.4% | -2,444.05 |
| Combined | 22 | 11 | 50.0% | +4,545.60 |

There is a final open bearish leg from 2026-09-07; it is excluded from those
closed-leg counts. Marked drawdown is 4,916.35 index points. These figures are
not account returns. Directional wins omit option fees and differ from the
previous cash-equivalent daily backtest's win rate, which included basis-point
fees. The bearish signal extension hurt this closed-leg index-point result.
Actual monthly put profitability remains unknown.

Historical option validation requires a date-correct monthly contract master,
lot sizes, complete strike availability at decision times, and option price or
bid/ask history covering entries, exits, rollover and valuation. Current option
quotes and today's instrument master cannot fill those historical gaps. Data
requirements and coverage evidence are saved in option_data_coverage.json.
