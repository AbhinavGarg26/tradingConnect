# Nifty 30m Midpoint EMA v1c10 — Version C10

Identity: `nifty_midpoint_ema`, current version `v1c10`. Nifty 15m v3 and the
separate opening-range breakout engine retain their existing rules.

## Fixed daily price levels

Aggregate the completed 09:15–09:44 IST one-minute candles. Use the first open O
and final close C, not candle highs/lows:

- M = (O + C) / 2
- B = abs(C - O) * 5 / 100
- CE price condition: completed minute close > M + B
- PE price condition: completed minute close < M - B

The body midpoint/thresholds stay fixed all day. The body buffer percentage
(default 5%) is independent of the new EMA buffer (default 2.5 index points).
A zero-size opening body produces coincident price thresholds.

## Current Version C10 rules (default `--logic price_cross`)

### C10: remove early-doji stop from active rules

`early_doji_stop` is disabled by default in both engine and backtest. A doji
after entry no longer moves the stop or independently triggers an exit. Retain
PE doji ENTRY filtering, the original crossing-candle midpoint stop, armed
two-close EMA12 exits, seven combined daily entries, and session square-off.
Run the existing command without `--early-doji-stop-points`. That option remains
available only for explicitly reproducing historical experiments; it defaults
to None. The next section documents that inactive historical behavior.

### Historical C9: profit-side stops on early post-entry doji (disabled)

After confirmed entry, inspect the first three fully held one-minute candles,
including the entry-fill candle if filled at its open. The signal candle is not
included. For a mid-minute fill, start with the next full candle. On a doji:

- PE: candidate stop = actual Nifty entry reference - 5 points; use min(old stop,candidate).
- CE: candidate stop = actual Nifty entry reference + 5 points; use max(old stop,candidate).

These are profit-side index levels. For CE entry 100, candidate stop is 105; for PE entry 100, candidate stop is 95. If the doji closes before price has moved beyond that level, signal immediate exit (CE close <=105; PE close >=95). Do not wait for a later profitable move. If no stop existed, activate the candidate. Never widen a stop. Keep a tightened
stop for the rest of the trade, and reset the window/entry price/flag after exit
or a new entry. This rule applies to both CE and PE; pre-entry doji rejection
still applies only to PE. Default doji threshold remains body <=10% of high-low
range; zero-range is doji. It uses the configured PE body-percent threshold,
or 10% if pre-entry PE filtering is disabled. A 13.77% body does not qualify
with the default 10% definition.

Observe doji only at candle close; activate the new stop then. Do not apply it
retroactively to that candle's high/low. If the observed close is already beyond
the new stop, signal exit immediately at that close. The backtest still fills
at the next minute open plus slippage; losses are not capped at five points.
Exit reason is `early_doji_stop` after this adjustment actually tightens risk. This supersedes C8, which put the stop five points on the loss side. Index-profit triggers do not guarantee a profitable fill or profitable option trade.
EMA exit and 15:15 square-off remain active.

Live callers should supply actual fill time and Nifty entry reference to
`confirm_fill(fill_time=..., index_price=...)`. Without these arguments the
engine falls back to the entry signal time/price for compatibility. Backtests
supply their simulated slippage-adjusted entry reference and fill timestamp.
CLI `--early-doji-stop-points 5` is default; `--no-early-doji-stop` reproduces C7.
The early-doji rule is unused by gap logic.

### C7: skip doji candles for PE entry

PE entry confirmation requires a non-doji candle as well as the existing
3-point distance and opening-body price filter. Default doji definition:
abs(close-open) <= 10% of (high-low). Equality counts as doji; zero-range
candles are doji. Both green and red non-doji candles may qualify; candle
color is not an additional rule. CE entries are unchanged.

A rejected doji does not consume a daily entry or clear the PE crossing setup.
Wait for the next or any later non-doji close meeting all filters, provided
price stays below its EMA36. Cancel the PE setup if price closes at or above
EMA36. The stop remains the midpoint of the candle before the original
crossing, not the candle before the eventual non-doji confirmation.

CLI `--pe-doji-body-percent 10` controls the threshold. Use
`--no-pe-doji-filter` to reproduce C6. Zero percent still rejects exact-zero
bodies; it does not disable filtering. This only affects price_cross logic.

### C6: maximum seven entries per day

Default `max_entries=7` / `--max-entries 7`, combined across CE and PE. Count
only confirmed entry fills, not exit orders or rejected/unfilled entry signals.
The seventh position still exits normally using stop, armed EMA12 exit, or
session square-off. After seven fills, block all new entries for the rest of
the session, regardless of side or crossing setup. Reset at the next reconciled
session. The cap applies to both price_cross and gap backtest modes; use
`--no-entry-limit` to reproduce historical uncapped comparisons. This supersedes
older descriptions below that say there is no daily entry cap. Restarted live
callers must restore the filled-entry count alongside positions.

### C5: crossing setup followed by distance confirmation

A fresh cross may confirm on the same candle or a later completed candle.
For PE, store a setup when the previous close >= its EMA36 and the current
close < its EMA36. Keep it active while subsequent closes remain below EMA36.
Enter when a completed close is at least 3 points below its EMA36 and below
the lower opening-body threshold. Cancel the setup on a close >= EMA36.
CE is symmetric: crossing above, confirmation >=3 points above and above the
upper threshold; cancel on a close <= EMA36. Exactly three points qualifies.

No entry while another position or order is pending. Crosses occurring during
that period are not queued. After exit or confirmed rejection, a new crossing
setup is required. Preserve the midpoint stop from the candle BEFORE THE
ORIGINAL CROSSING, not the candle before the later confirmation. Do not move
the stop during confirmation. Decisions use full precision.

`--entry-distance-points` defaults to 3 for price_cross; it is unused by gap
logic. This supersedes C4's same-candle distance requirement and any older
fresh-cross-only description below. The new entry reason is
`cross_confirmed_ema36`. The backtest still fills at the next candle open.

### Candle-body stop added in C3

If candle 2 generates the fresh EMA36 crossing entry, use candle 1 immediately
before it: stop = (candle 1 open + candle 1 close) / 2. Use neither candle 2's
body nor candle 1's high/low. The entry signal includes `stop`. Activate it only
after confirmed entry, keep it fixed while holding, clear it on confirmed exit,
and recalculate it for each new crossing entry. This applies symmetrically:

- PE stop triggers on a completed minute close >= the stop.
- CE stop triggers on a completed minute close <= the stop.

The stop is active even before the EMA12 exit has armed and takes precedence
over the two-close EMA exit (session-end retains first priority). It does not
wait for two closes. Intrabar high/low touches alone do not trigger this
close-based engine. Backtest execution remains the following minute open with
slippage, not an assumed exact stop fill. Gaps can therefore exceed the planned
stop distance. This is an index trigger, not a broker-native option stop order.
The literal prior-body midpoint is used even if an entry gap has already moved
price beyond it; the first subsequent qualifying close will trigger exit.

Default candle stop is enabled for price_cross. Use `--no-candle-stop` to
reproduce C2. Older `--logic gap` versions do not use this stop. Signal output
includes the candle-stop level. `No fixed stop` elsewhere refers to the old
fixed-point stop: the new candle-body stop is additional to the C2 EMA rules.

Retain the fixed 30-minute body thresholds above. Compare completed one-minute
closes with each candle's own EMA36:

- CE entry: previous close <= previous EMA36, current close > current EMA36,
  AND current close > upper body threshold.
- PE entry: previous close >= previous EMA36, current close < current EMA36,
  AND current close < lower body threshold.
- CE: first arm the exit on a completed post-entry candle closing strictly above its EMA12; then exit on two consecutive closes strictly below their own EMA12.
- PE: first arm the exit on a completed post-entry candle closing strictly below its EMA12; then exit on two consecutive closes strictly above their own EMA12.

The entry signal candle does not arm an exit. Closes before the arming candle do not count toward exit. Equality does not arm. Once armed, the flag stays armed until confirmed exit; equality with EMA12 or any nonqualifying close resets only the consecutive exit count to zero. The 15:15 square-off applies even if the exit never armed.
Price-threshold failure alone no longer exits a position. Every re-entry needs
another fresh EMA36 crossing; a crossing rejected by the body filter is not saved
for later. Crosses during another held/pending position are not queued. Reset
exit count and arming flag after confirmed entry/exit and at a reconciled new session. Pending
order candles do not count toward a not-yet-confirmed position's exit sequence.

No cooldown, EMA-gap requirement, fixed stop or target. Retain the 15:15 daily
square-off, next-minute-open backtest fills, 36-close daily EMA warm-up, full
precision decisions and two-decimal EMA display. Thus the earliest signal is
09:51, and the first eligible minute still needs a fresh crossing. The engine
and backtest default to this logic. For this mode the EMA-gap argument is unused;
a positive cooldown is rejected.

```sh
python3 -m backtest.nifty_midpoint_ema --fetch --from-date 2026-07-01 --to-date 2026-10-19 --logic price_cross --slippage-points 1 --summary-only
```

The rules below describe earlier versions, available using `--logic gap`.

## Previous Version B EMA rules (`--logic gap`)

Let G = EMA12 - EMA36 and T = 2.5 index points:

- Enter CE when the CE price condition holds and G >= +T.
- Enter PE when the PE price condition holds and G <= -T.
- Exit CE when its price condition fails OR G <= -T.
- Exit PE when its price condition fails OR G >= +T.
- Between -T and +T, hold an existing position if its price condition still holds;
  remain flat otherwise. Reaching the opposite boundary triggers an EMA exit.

This deliberately uses separate entry/exit thresholds: an owned CE does not
exit merely because G falls below +2.5. Equality at a price threshold fails that
price condition. Comparisons use full precision; displayed EMAs have two decimals.

No cooldown in Version B. After confirmed exit, a subsequent completed minute
may signal entry if conditions hold. One position at a time; no same-signal-bar
reversal, fixed stop, target, trailing stop, risk cap or daily entry cap.
Square off at the first close at/after 15:15 IST and disallow further entries.

## EMA convention and live integration

EMAs reset daily, seed with the 09:15 close, and update with alpha=2/(period+1)
(adjust=False). Require 36 completed closes: earliest signal 09:51. These may
differ from charting platforms that carry previous-session EMA history.

`NiftyMidpointEMA.on_bar(bar)` accepts completed consecutive one-minute Bar
objects with aware interval-start timestamps. Signal timestamps mark closes.
Missing candles must be backfilled; replay from 09:15 on late startup without
executing retrospective signals. Pending signals suppress duplicate orders.
Call `confirm_fill(fill_time=actual_fill_time)` after confirmed order completion.
Call `reject_signal()` only when no fill is established; uncertain orders need
reconciliation. Persist state and reconcile positions before resuming live.

Reuse `current_atm_option` in `strategies.nifty_opening_range` to select the nearest
listed unexpired NIFTY expiry and closest strike at entry. Use the contract's
lot size and retain that contract until exit. This engine is not connected to a
live broker runner; existing live commands still run their original strategies.

## Backtesting and comparison

CSV columns: `date,open,high,low,close`, with one-minute interval-start dates.
Naive dates are interpreted as IST. Each session starts 09:15 and has consecutive
minutes. Fetch requests minute history using the configured Kite session.

Previous Version B:

```sh
python3 -m backtest.nifty_midpoint_ema --fetch --from-date 2026-07-01 --to-date 2026-10-19 --logic gap --ema-gap-points 2.5 --cooldown-minutes 0 --slippage-points 1 --summary-only
```

Version A reproduction:

```sh
python3 -m backtest.nifty_midpoint_ema path/to/nifty_1m.csv --logic gap --ema-gap-points 0 --cooldown-minutes 5 --slippage-points 1 --summary-only
```

Original baseline: `--logic gap --ema-gap-points 0 --cooldown-minutes 0`. Zero-gap mode
preserves the original strict EMA entry comparisons and exits at EMA equality.
Other combinations are labeled custom. Defaults: buffer-percent 5,
ema-gap-points 2.5, cooldown-minutes 0, slippage-points 0.

Version A waits five minutes from confirmed exit fill before either side can
re-enter. An aware exit fill timestamp is required when cooldown is positive.
Rejected exits do not start cooldown. All versions continue updating EMAs every
minute and preserve the same daily square-off.

Signals fill at the next minute open with adverse index slippage per side.
Include the 15:15 candle open for the square-off fill. Final pending/open trades
are reported, not forcibly marked closed. Unresolved trades cannot cross sessions.
Without --summary-only the report includes signal conditions and completed trade
fills with signed index-point gains/losses. CE P&L = exit-entry; PE = entry-exit.
Summary includes win rate, net/gross points, expectancy, profit factor, drawdown,
CE/PE and exit-reason breakdown. R-multiple is N/A (no predefined stop).
Results are index proxies, not option-premium/rupee P&L, and exclude actual fees
and taxes. Option performance requires historical contract quotes and fills.
