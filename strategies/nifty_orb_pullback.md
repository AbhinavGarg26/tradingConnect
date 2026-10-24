> Status: discarded from active research at the user's request. Retained only as historical work. The new positional baseline is documented in `strategies/nifty_daily_ema.md`.

# Nifty opening-range first-pullback — research version 1

This is an unvalidated hypothesis, not a strategy with an established 70% win
rate. It tests Nifty underlying signals, not Nifty option profitability. It does
not place orders or connect to the live trading engines.

## Run

From the project root, using a CSV:

```sh
venv/bin/python -m backtest.nifty_orb_pullback candles_5m.csv
```

Or read Nifty minute candles from `market_snapshots`, using the same loader and
UTC-to-IST convention as the Nifty 30-minute reference backtest:

```sh
venv/bin/python -m backtest.nifty_orb_pullback --database --from-date 2026-09-01 --to-date 2026-10-23
```

Database mode selects `NIFTY 50` / `1m` and aggregates complete five-minute
buckets aligned to 09:15 IST. It uses the existing local database configuration;
no CSV or Kite login is needed. `--fetch` is retained as an alias for `--database`.
This strategy uses database-only mode: it never calls Kite or writes snapshots.
Missing/invalid minutes fail the run by default. Add `--skip-incomplete-days`
to explicitly omit whole affected sessions; excluded dates are printed. The
existing 30-minute reference loader retains its original repair behaviour.
Database holidays and the shared regular-session calendar are respected.
The runner writes `outputs/nifty_orb_pullback_trades.csv` and prints trade count,
win rate, net index points, profit factor, average R, and maximum closed-trade
point drawdown. Undefined ratios are printed as null. Drawdown does not include
unrealised intratrade losses.

Optional settings:

```sh
venv/bin/python -m backtest.nifty_orb_pullback candles_5m.csv --reward 2 --slippage 0.5 --cost-points 0 --output outputs/orb_research.csv
```

`--slippage` is adverse underlying points per fill. `--cost-points` is an extra
round-trip underlying-point deduction. Neither is an option brokerage/tax model.
Defaults are research assumptions, not calibrated execution estimates.

## Input

CSV header: `date,open,high,low,close`; add `volume` only for futures VWAP.
Timestamps are candle **opening** times, ISO format. Naive times mean IST;
offset-aware times are converted to IST. Supply sorted, unique five-minute
candles, all 75 bars from 09:15 through 15:25 for each included regular session.
Missing bars, partial sessions, malformed OHLC, and duplicate timestamps are
rejected. Exclude special shortened sessions. Entire missing dates cannot be
detected without an exchange calendar, so verify source coverage separately.

Use Nifty spot without VWAP. To test futures, supply genuine futures candles
and `--use-vwap`; volume must be positive. Do not mix spot levels with futures
fills. Avoid artificial rollover jumps in futures data; handle contract changes
before testing. Include warmup history: EMA signals require 23 completed
15-minute bars; ATR requires 14 five-minute bars. EMA/ATR carry across sessions,
and overnight gaps enter ATR. The opening range and VWAP reset daily.

## Frozen default rules

- Opening range: six candles starting 09:15 through 09:40, known at 09:45.
- Bullish trend: last completed session-aligned 15-minute close above its EMA(20),
  and EMA above its value three completed 15-minute bars earlier. Reverse for
  bearish. EMA starts at the first 15-minute close, with 23-bar warmup.
- Optional futures filter: five-minute close above session typical-price VWAP
  for longs, below for shorts. Spot has no VWAP filter.
- First five-minute close beyond the relevant opening boundary after 09:45 is
  the breakout. It must pass trend/VWAP filters. A failed first breakout consumes
  that direction for the day. The breakout candle cannot also be the pullback.
- Over the next six candles, require a candle range to intersect the boundary
  plus/minus 0.15 ATR(14). Cancel if a close breaches the boundary against the
  trade by more than 0.25 ATR. ATR uses Wilder smoothing and completed candles.
- After touching, confirmation must close on the breakout side, in the trade
  direction relative to its open, and in the top quarter of its range for longs
  or bottom quarter for shorts. Trend/VWAP must still pass. Touch and confirmation
  may occur on the same candle.
- Submit a stop entry one 0.05-point research tick beyond confirmation high/low,
  eligible only on the next two candles. Gap entries fill at the worse of open
  and trigger, plus adverse slippage. The tick is a configurable index-simulation
  increment, not a claim about current exchange contract specifications.
- Stop: extreme of all post-breakout candles through confirmation, buffered by
  0.10 ATR. Planned and actual entry risk must lie within 0.5–1.5 confirmation
  ATR, otherwise skip. There is no retry after confirmation.
- Fixed target: 2 times actual entry-to-stop risk. No scaling, trailing, or
  break-even stop adjustments.
- Entries may occur from 09:45 until strictly before 11:30 IST. Pending orders
  cancel at 11:30. Exit remaining positions at the 15:15 candle open.
- One setup per direction, maximum two entries per day, one position at a time.
  Stop taking entries after two losing trades. Unfilled orders expire, and all
  setup state resets daily.

## Fill assumptions

Orders only act after their signal candle closes. Within an ordinary holding
candle, stop takes precedence if both stop and target are touched. A gap through
the stop fills at the worse opening price plus slippage. Targets receive no
favourable gap improvement. On an entry candle, a touched stop is assumed hit
(even if the low/high might have preceded entry); targets are not awarded until
a subsequent candle reaches them. These are conservative approximations, not
reconstructed tick paths. Entry/exit timestamps identify the containing candle,
not an exact intrabar execution time. Use finer data to resolve sequencing.

## Evaluation

Use earlier data for development and untouched later dates for evaluation;
freeze rules before that evaluation. Include several market regimes and report
trade counts alongside win rates. R is net underlying points divided by initial
underlying risk. Costs, gap losses and time exits mean actual R need not be +2/-1.
Run sensitivity checks on execution assumptions, not a large parameter search
to force a 70% result. The synthetic unit checks establish simulation behaviour,
not historical profitability.

The intended ₹1 lakh account and ₹500 planned option risk are **not** simulated.
Do not multiply index points by an option lot size to estimate option P&L. A later
option execution backtest needs dated strikes, expiries, lot sizes, historical
premiums/spreads and costs, plus affordability checks. Only that stage can test
whether one option lot fits the budget and whether the option trade wins.

## Controlled improvement experiments

The default remains the frozen baseline. Two individually changed variants are
available; neither is claimed to improve returns before real-data evaluation:

- `--variant qualified_breakout`: a close beyond the opening boundary which
  fails trend/VWAP no longer consumes that direction. Wait for the first close
  beyond that boundary which passes the filters, then use the same six-candle
  pullback window. No repeated attempts after an accepted breakout.
- `--variant wider_stop`: change only the pullback stop buffer from 0.10 to
  0.25 ATR. Keep the same 0.5–1.5 ATR risk bounds and 2R target. This may reject
  more entries or delay targets; a wider stop does not guarantee better results.

Compare all three on identical input:

```sh
venv/bin/python -m backtest.nifty_orb_pullback --database --from-date 2026-09-01 --to-date 2026-10-23 --compare-variants --output outputs/nifty_orb_research.csv
```

The trade CSV contains the baseline in comparison mode. A sibling
`nifty_orb_research.comparison.json` records every variant's full-sample metrics,
first-70%-of-sessions development metrics, remaining-session validation metrics,
and setup rejection counts. Indicators retain earlier history across the split.
Run a selected variant separately to export its trades. No automatic selection
or parameter sweep is performed. The already-inspected September–October sample
is exploratory, including its chronological split; use new untouched dates to
validate any choice. Trade count and realised payoff must accompany win rate.

Diagnostics count accepted breakouts, failed filter checks, pullback expiry or
invalidation, confirmations, risk rejections, order expiries, and filled entries.
In the qualified-breakout variant, failed-filter counts are repeated candle checks,
not unique breakout events. Low trade counts remain a limitation, not a reason
to lower costs or execution assumptions.

## Selective setup for fewer trades

`--variant selective` keeps the original stops, 2R target, EMA trend, first
breakout rule, and pullback confirmation. It additionally requires:

1. The opening 30-minute net move (09:40 candle close minus 09:15 open) is at
   least 60% of that opening range's high-low width, in the trade direction.
2. The first breakout candle's directional body is at least 60% of its high-low
   width, and its close is at least 0.10 completed ATR beyond the range boundary.
3. Maximum one filled trade per day. Rejected first breakouts are not retried.

These are fixed candidate rules, not measured probabilities or a promise of
higher accuracy. More filters can reduce sample size without raising win rate.
The comparison report includes this variant alongside the previous experiments.
Use a separate output path when exporting its trades:

```sh
venv/bin/python -m backtest.nifty_orb_pullback --database --from-date 2025-01-01 --to-date 2026-10-23 --variant selective --output outputs/nifty_orb_selective_trades.csv
```

Historical periods examined during this conversation are research data. Any
candidate chosen from these results still requires untouched forward data.

## Frozen baseline and frequency research

The agreed baseline is now named **selective_v1**, stored explicitly in
`strategies/nifty_orb_selective_v1.json`. Preserve that file when changing
strategy defaults. The older CLI name `baseline` still refers to the original
unfiltered experiment for compatibility; use `--variant selective` for the
agreed setup, or the dedicated frequency runner below for its frozen config.

The frequency runner loads the frozen file, records its SHA-256 fingerprint,
and tests six predeclared changes against it. All retain 2R, the one-trade daily
cap, trend alignment, the original stops, and the same execution assumptions:

| Candidate | Change relative to selective_v1 |
| --- | --- |
| opening_40pct | Opening directional displacement threshold 60% -> 40% |
| breakout_body_40pct | Breakout directional body threshold 60% -> 40% |
| retest_030atr | Breakout-level touch band +/-0.15 -> +/-0.30 ATR |
| pullback_12bars | Allow 12 rather than 6 post-breakout bars for the first pullback |
| wait_strong_breakout | Ignore weak breakout closes until the first strong one; still consume a failed trend filter |
| opening40_retest030 | The explicitly predeclared combination of the opening and retest relaxations |

```sh
venv/bin/python -m backtest.nifty_orb_frequency --database --from-date 2025-01-01 --to-date 2026-10-23
```

Outputs: `outputs/nifty_orb_frequency/comparison.json` plus a separate trades CSV
for every candidate, including the frozen baseline. The report includes monthly
and yearly results, a 70/30 chronological breakdown, trade frequency, descriptive
win-rate intervals, baseline-entry retention, and results of newly added entries.
Entry matching uses direction and timestamp, not a claim of economic equivalence;
a wider retest can alter a later entry. Previously examined dates are not an
untouched holdout. No automated winner is selected and no relaxed rule is promoted
to the baseline. The 2025–October 2026 baseline must reproduce 9 trades and
203.62799723570424 index points on the same data.

Research observed on 446 sessions: opening relaxation added 7 trades at 28.6%
wins; weaker breakout added 4 at 25%; wider retest added 4 at 25%; longer pullback
added 3 at 33.3%; waiting for a strong breakout added 6 at 16.7%; the combination
added 12 at 25%. These small samples do not establish stable probabilities.
The longer-pullback candidate had 12 trades, 50% wins, +222.45 points and 65.29
points closed-trade drawdown versus the baseline's 9 trades, 55.6% wins, +203.63
points and 37.23 points drawdown. This is a frequency/payoff trade-off, not a
proven improvement in accuracy. Full candidate outcomes are retained in JSON.

For earlier history with known source gaps, the frequency runner also accepts
`--skip-incomplete-days`; it records the exact excluded dates under
`excluded_incomplete_days` in its report. Without that explicit flag it stops
at the first incomplete session. Data remains read-only in either mode.
