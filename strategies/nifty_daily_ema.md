# Daily Nifty EMA crossover — positional research baseline

Replaces the discarded intraday opening-range strategy for active research.
Default: EMA(20) and EMA(50) on completed Nifty daily closes, long-only.
This is a new hypothesis with no target or guaranteed win rate.

## Rules

1. Calculate both EMAs on daily closes. Each EMA starts at the first close using
   alpha = 2/(period+1). Wait until at least 50 daily observations before signals.
2. Bullish crossover: yesterday's fast EMA <= slow EMA and today's fast EMA >
   slow EMA. After today's daily candle is complete, schedule a long entry for
   the next available trading session's open. Do not enter retrospectively if
   the fast EMA was already above the slow EMA when warmup ended.
3. Bearish crossover: yesterday's fast EMA >= slow EMA and today's fast EMA <
   slow EMA. Exit the long at the next available trading session's open.
4. Hold positions across sessions. No short trades, scaling, intraday target,
   fixed stop, or forced daily exit. Stay in cash while flat; cash earns no return.
5. A final pending signal remains unfilled if the next daily open is absent.
   Final open positions are marked to the last close and reported separately;
   they are not forced closed or included in closed-trade win rate.

Unlike the previous setup, there is no predefined R multiple or per-trade loss
cap. The exit follows the trend reversal; overnight gaps and whipsaws are part
of the model. A stop-loss overlay would be a separate experiment, not part of
this two-EMA baseline.

## Data and execution

Database mode reuses the Nifty 30-minute reference backtest's market_snapshots
loader in database-only mode. Read NIFTY 50 / 1m, convert database UTC to IST,
then aggregate each full 09:15–15:29 session into one daily OHLC candle.
No Kite fetches or database writes occur. Missing/invalid sessions stop the run
unless explicitly excluded with --skip-incomplete-days. Exact excluded dates
are recorded. Excluding a day changes EMA history and can delay fills across a
missing open, so these results are conditional on retained data. For a final
validation, repair source gaps or use a verified daily data series.

CSV mode expects date,open,high,low,close at daily frequency, chronological and
unique. It can also be used for adjusted stock/ETF data; corporate-action
adjustments and dividends must be handled by the supplied data. No trading-day
calendar or completeness inference is applied to daily CSVs.

Start with 100,000 hypothetical account units. When entering, invest the whole
balance using fractional index units; sell the whole position at exit. Costs
are deducted at each fill (default 5 basis points per side) and optional adverse
slippage is applied to the next session open. These are configurable research
assumptions, not measured charges. The index itself cannot be bought directly:
this is not an executable option, futures, or ETF backtest. It includes no
option expiry/decay, futures rollover, ETF tracking error, dividends, or account
taxes. Never interpret this result as the P&L of holding Nifty calls.

Daily-close equity includes unrealised position value and actual simulated
entry/exit costs, without deducting a hypothetical exit fee on open positions.
Drawdown uses daily-close account equity, including initial cash as the initial
peak; it does not measure intraday drawdown. Buy-and-hold begins at the open of
the session after slow-period warmup, pays the same entry assumptions, and is
marked to the same final close. It is not a risk-matched benchmark.

## Run

```sh
venv/bin/python -m backtest.nifty_daily_ema --database --from-date 2020-01-01 --to-date 2026-10-23 --skip-incomplete-days
```

To change periods explicitly (no automatic parameter optimisation):

```sh
venv/bin/python -m backtest.nifty_daily_ema --database --from-date 2020-01-01 --to-date 2026-10-23 --skip-incomplete-days --fast 10 --slow 30 --output-dir outputs/nifty_daily_ema_10_30
```

Or use a verified daily CSV:

```sh
venv/bin/python -m backtest.nifty_daily_ema --csv 'resources/historical_data/NIFTY 50.csv'
```

The runner saves summary.json, trades.csv, signals.csv, and equity.csv in
outputs/nifty_daily_ema by default. Empty trade/signal files are empty rather
than invented entries. The summary reports closed-trade win rate, profit
factor, account return, daily-close drawdown, holding duration, final position,
final pending signal, and buy-and-hold return. A high win rate is not assumed:
trend systems can have repeated small losses and a few large winners. Judge
this hypothesis using return, drawdown, exposure and holding duration as well
as trade count, and validate frozen rules on additional data.

## Compare the five requested pairs fairly

```sh
venv/bin/python -m backtest.nifty_daily_ema --database --from-date 2020-01-01 --to-date 2026-10-23 --skip-incomplete-days --compare-pairs --evaluation-start 2021-01-01 --output-dir outputs/nifty_daily_ema_comparison
```

Pairs: 9/21, 10/30, 20/50, 20/100, and 50/200. Database history is loaded once.
The 2020 observations initialise EMAs; all accounts start flat on the same first
available evaluation date and wait for a fresh crossover. Warmup crosses do not
create positions. This differs from the original pair-specific start dates,
so the comparison's 20/50 return need not match the earlier standalone result.
Without --evaluation-start the comparison uses the first session after 200
prior candles. The same hypothetical costs, capital, calendar, input exclusions,
next-open fills and final marking apply to every pair and its buy-and-hold
benchmark. Summary tables, complete daily input and per-pair trades/signals/equity
are saved under the output directory. CAGR uses elapsed calendar time between
first and final evaluation closes. Open positions affect total return but not
closed-trade win rate. Comparing five pairs does not independently validate the
pair with the best historical return.
