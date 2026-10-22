# Nifty fixed opening-range strategy

Implementation: `strategies/nifty_opening_range.py`. This is a signal engine;
it does not submit orders and is not connected to the existing live runner.

Use the completed 09:15–09:30 IST Nifty index candle as the fixed daily range.
A 30-minute reference is also supported with `reference_minutes=30`, provided
the caller supplies the completed 09:15–09:45 candle. Never replace this range
with later candles. Feed fresh, chronological Nifty quotes after the range
has completed. A strict breach above the opening high signals BUY_CE; a
strict breach below the low signals BUY_PE, including a first breach hours
later. Equality does not trigger. A late start evaluates the first fresh quote
against the opening range; it does not reconstruct earlier trades.

Use `current_atm_option` at entry to select the nearest strike of the indicated
side in the nearest listed expiry on or after the session date. It reads actual
listed contracts rather than assuming an expiry weekday or fixed lot size.
The execution caller must obtain the contract lot size and confirm order fills.

The confirmed default is `risk_mode='index'`: stop 5 Nifty points from the
confirmed entry's index quote, and target 30 Nifty points from that quote.
For example, a CE entry at Nifty 25,000 has stop 24,995 and target 25,030;
a PE entry at 25,000 has stop 25,005 and target 24,970. These are index
triggers, not option-premium levels.

The supported risk modes are:

- `premium`: confirmed option buy premium minus 5; target premium plus 30, for both CE and PE.
- `index`: CE stop entry Nifty minus 5, target plus 30; PE stop entry Nifty plus 5, target minus 30.
- `opposite_range`: CE stop opening low minus 5; PE stop opening high plus 5. Targets remain 30 Nifty points from entry.

Call `confirm_entry(index_price=..., option_fill=...)` only after a confirmed
buy fill. Premium mode requires fresh quotes for the selected option along
with Nifty quotes. Exit signals occur on the first observed quote at or beyond
the stop or target. Actual fills can differ from trigger levels. A pending
signal suppresses duplicate signals. Call `confirm_exit()` after the sell fill;
call `reject_signal()` only after establishing that no fill occurred.

Default: one trade per session, one position at a time, fixed stop and target,
no trailing. `max_entries` allows an explicit higher limit; after an exit a
fresh quote outside the same opening range can signal another entry without
requiring a return inside the range. New entries stop at 15:15 IST; an open
position signals a session-end exit on the first quote at or after 15:15.

The caller must supply the correctly aggregated opening candle, fresh quotes,
holiday/session handling, order execution, position persistence and restart
reconciliation. This module alone is not a live trading service. Existing
`nifty_15m_live.py` continues to use its original strategy.

## Identity and backtesting

Display name: **Nifty Opening Range Breakout (ORB) v1**.
Strategy ID: `nifty_opening_range_breakout`; version: `v1`.
This is separate from Nifty 15m v3.

The backtest shares v3's `TradeResult`, `summarize`, `print_summary`, and Kite
history loader. It reports trades, win/loss/breakeven, net and gross index
points, win rate, average win/loss, expectancy, profit factor, payoff ratio,
average initial risk, average R, closed-trade drawdown, CE/PE breakdown and
exit reasons. These results exclude historical option P&L, fees and taxes.

```sh
python3 -m backtest.nifty_opening_range path/to/nifty_15m.csv --summary-only
python3 -m backtest.nifty_opening_range --fetch --from-date 2026-09-01 --to-date 2026-09-30 --summary-only --reference-minutes 15 --stop-points 5 --target-points 30 --max-entries 1 --slippage-points 1
```

Use the Python environment with project dependencies installed. CSV columns:
`date,open,high,low,close`. Candle timestamps represent interval starts;
naive timestamps are interpreted as IST. Every session must begin at 09:15
and have consecutive 15-minute bars. Include bars through the 15:15 exit
observation (the close of the 15:00 bar). An unfinished final trade is reported
as open; an unfinished trade before another session causes an error.

**This OHLC backtest is a close-sampled approximation.** It observes each bar
only at its close after the opening range. It cannot measure intrabar tick
breakouts or the actual performance of a 5-point live stop. No high/low price
path or exact stop fills are invented. Slippage is an adverse index-point
proxy on entry and exit, and stop/target levels are based on the adjusted
entry. Stops can lose more than 5 points when the next observed close crosses
them. Tick replay is needed to evaluate the intended continuous rule.

Parameters: `--reference-minutes` 15/30, `--stop-points` (5),
`--target-points` (30), `--max-entries` (1), `--slippage-points` (0),
`--summary-only`, CSV or `--fetch --from-date --to-date`.
V3's previous-candle references, prior-entry filters, trailing rules and
30-point risk cap do not apply to ORB. The fill assumption is explicitly
close-sampled rather than v3's next-open model.

## Shared engine for live integration

```python
from strategies.nifty_opening_range import NiftyOpeningRange, current_atm_option
strategy = NiftyOpeningRange(stop_points=5, target_points=30)
strategy.set_reference(opening_bar, observed_at=range_close_time)
signal = strategy.on_tick(quote_time, nifty_ltp)
# On BUY: select current_atm_option(...), submit and confirm the buy,
# then strategy.confirm_entry(index_price=nifty_at_fill, option_fill=filled_premium).
# On EXIT: sell the owned contract, confirm the fill, then strategy.confirm_exit().
```

The live function is reusable, but a broker runner is still required. Do not
use `nifty_15m_live.py --live` for ORB: that remains the v3 strategy. ORB has no
standalone live command yet. Its state is in memory; the caller must persist
and reconcile orders and positions before resuming after a restart.
