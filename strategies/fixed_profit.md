# Fixed Profit v2-sl15-fixed-entry — opening-range support and resistance

Uses NIFTY index points. Target is fixed at 30 points; stop remains fixed at
15 points from the actual fill. No trailing. This is a signal engine and
research backtest; it does not submit orders or enable a live service.

Default research candidate: `setup_mode='retest'`, `confirmation_minutes=5`,
`min_range_points=40`, `tolerance_points=2`, `follow_through=False`, `stop_points=15`. This was the
training leader within the retest family. The overall training winner was a
rejection model which failed later validation. See
`outputs/fixed-profit-v2/analysis.md` for both, including costs and dated trades.

## Opening reference

Use only the completed 09:15–09:45 IST candle. Freeze its high H and low L for
the session. Opening range H−L must strictly exceed the configured minimum
(20 or 40 points in the comparison). Never update these levels intraday.

## Two testable ways a level can hold

**Break and retest (`setup_mode='retest'`).** A completed candle closes more than
2 points above H or below L. On a later candle within 30 minutes, price revisits
the broken level within the configured tolerance and closes directionally back
on the breakout side. H now holding as support gives CE; L holding as resistance
gives PE. The breakout candle cannot count as its own retest.

**Original-level rejection (`setup_mode='rejection'`).** A bullish candle tests L
within tolerance and closes back inside the opening range: potential CE. A
bearish candle tests H and closes back inside the range: potential PE.

With `follow_through=True`, a still later candle must close beyond the rejection
candle's high for CE, or low for PE, within 15 minutes. The rejection's adverse
wick must not be breached, and the close must remain on the intended side of
the reference. With `False`, the rejection close is the confirmation itself.

A missing confirmation candle clears setup state. A failed close or expired
setup requires fresh confirmation. Candle periods tested: 1, 3 and 5 minutes,
anchored at 09:45. Tolerances tested: 2 and 5 index points.

## Entry, risk, and exits

- Signal only after the confirmation candle is fully closed. Backtest enters at
  the next minute's open; actual execution requires confirmed fills.
- Do not chase more than 10 points from the reference level.
- Entry qualification still requires a hypothetical 10-point stop at least
  1 point beyond the rejection candle's adverse wick. Actual exits use 15 points. If it does not, skip the trade rather than widen the stop.
- CE: target = actual index entry + 30; stop = entry − 15.
- PE: target = actual index entry − 30; stop = entry + 15.
- At most one confirmed trade per day. No fresh confirmation at/after 14:30 IST.
  Exit any remaining position at 15:15 IST.
- No trade is a valid outcome. Do not infer the day's direction beforehand.

## Engine interface

`FixedProfit` in `strategies/fixed_profit.py` accepts configuration for minimum
range, confirmation minutes, tolerance, setup mode, and follow-through.
`set_reference` receives a completed Decimal-valued OHLC `Bar`. `on_bar` receives
completed confirmation candles and their observation time. `confirm_entry`
validates the actual index fill before setting fixed risk. `on_tick` manages
exits only. `confirm_exit` / `reject_signal` resolve pending signals; callers own
contract selection, broker fills, price-stream continuity, and protection.

This replaces the elapsed-hold entry interface. The old implementation and
comparison remain in `strategies/fixed_profit_hold.py` and
`backtest/fixed_profit_hold.py` for reproducibility.

## Run the historical comparison

From the project directory:

```bash
python3 -m backtest.fixed_profit /tmp/fixed_profit_candles.csv --compare-stops --output outputs/fixed-profit-sl15-fixed-entries
```

Input columns: `date,open,high,low,close`; naive timestamps are database UTC.
The input is an existing local database export; a temporary file may need to be
exported again after cleanup. No market-data fetch or database writes occur.

The command above uses the original 10-point entry qualification with both
10- and 15-point actual exit stops. Outputs are `stop_comparison.json`, `baseline_sl10_trades.csv`,
and `baseline_sl15_trades.csv`. Entry dates, sides, times and prices must match between stops. Only the actual
stop distance and resulting exits change. The previous 359-trade full rerun is
retained as a separate historical experiment, not the current baseline.
The old 10-point reports and EMA experiment remain historical; the EMA engine
and diagnostic runner explicitly retain their original 10-point configuration.

Without `--compare-stops`, the research grid evaluates 48 predefined configurations
using `--stop-points` (default 15). It selects the highest
mean gross points on 2020–2023 with at least 100 training trades, and reports
2024–2025 and 2026 separately. Later-period performance does not select the
configuration. The study is retrospective, not a prospective live test.

Outputs: `analysis.json` (all comparisons and exclusions), `trades.csv` (overall
training winner, the rejection model), and `retest_trades.csv` /
`rejection_trades.csv` (each family's training leader). The accompanying
`analysis.md` research report describes the retest candidate and why neither
model establishes a robust edge.
Backtest uses next-open entries, minute stop/target checks with stop first when
both occur, worse fills on stop gaps, and a 15:15 open exit. Exact intraminute
exit timestamps cannot be inferred. Costs and option returns are not observed;
index-point cost deductions are illustrative sensitivity checks.

Support/resistance background: [Zerodha Varsity](https://zerodha.com/varsity/chapter/support-and-resistance/).
The precise rules above are research hypotheses, not rules endorsed by that source.
