# Gap-Up Strategies v1 — 15-minute confirmed

This package contains two separate decision engines. A stock gap and an index
gap are not treated as the same setup.

## 1. Stock catalyst gap continuation

Strategy key: `stock_catalyst_gap_continuation`

### Universe

- The production research universe is the current **NIFTY 500** constituent
  list. Symbols are loaded through `backtest.equity_universe` using the
  `nifty500` index key and are deduplicated before evaluation.
- Current constituents introduce survivorship bias. A research report must
  disclose this until point-in-time NSE membership is available.
- Membership alone is not a liquidity filter; the traded-value and minimum
  price requirements below still apply.

### Pre-open qualification

- Long equities only.
- Price must be at least ₹50 and the caller must confirm adequate traded-value
  liquidity.
- Opening gap must be 2–8% above the previous adjusted close.
- Open must be above the previous session's high.
- A verified material catalyst is mandatory. Examples include results,
  guidance, an exchange-filed order win, regulatory approval, acquisition or a
  material capacity announcement. Text classification and disclosure sourcing
  belong to the caller; the engine accepts a boolean only.
- The previous 20-session return must not exceed 30%.
- The opening price must not be more than 15% above EMA20.

### First 15-minute confirmation

- Wait for the completed 09:15–09:30 bar. Never trade an unfinished candle.
- Opening relative volume must be at least 1.5× the median volume for the same
  15-minute window over the previous 20 sessions. At least ten valid historical
  observations are required.
- The bar must close above VWAP and in the top quarter of its range.
- Upper wick must be no more than 35% of the bar's range.
- At least 60% of the overnight gap must remain at the close.
- The bar must not violate the previous day's low.
- Both the broad market and the stock's sector must be aligned upward.

### Entry and risk

- Submit a stop-entry at the completed opening bar's high.
- Initial stop is the higher of opening-bar low and VWAP.
- Reject risk wider than 3% of the entry price.
- The engine returns 1R and 2R target levels. Execution callers must use
  adverse gap-through fills and must never assume a stop fills at its trigger.

## 2. NIFTY gap regime

Strategy key: `nifty_gap_regime`

### Qualification

- Opening gap must be 0.40–1.50% above the previous close.
- Wait for the completed first 15-minute bar.
- Breadth means the fraction of current NIFTY 50 constituents above their
  session VWAP. Breadth must be calculated without stale constituents or stale
  quotes.

### Continuation regime

- Opening bar closes above VWAP and in the upper third of its range.
- At least 60% of the gap remains.
- At least 60% of constituents are above VWAP.
- Buy-stop trigger: opening-range high.
- Stop: the higher of VWAP and the opening-range midpoint.

### Rejection regime

- Opening bar closes below VWAP and below its midpoint.
- It has returned below the previous session's high.
- No more than 40% of constituents are above VWAP.
- Sell-stop trigger: opening-range low.
- Stop: opening-range high.

If neither regime confirms, there is no trade. The strategy does not assume
that every gap continues or every gap fills.

## Shared execution rules

- Signals use completed candles. A trigger touched during the candle that
  created the signal is not a fill.
- A pending entry becomes active only on later observed prices.
- Cancel pending entries if not filled by 11:30 IST.
- Maximum one position per symbol and one NIFTY gap-regime trade per session.
- At 1R, callers may take a partial and move the stop only after a new completed
  structural higher/lower low confirms. At 2R, take another partial or exit.
- Close intraday positions by 15:15 IST.
- Use observed bid/ask or conservative slippage. Index-point results are not
  option-premium results.
- Corporate-action-adjusted previous prices are mandatory for equities.

## Python API

The implementation is in `strategies/gap_up.py`:

- `stock_gap_watch(...)`
- `confirm_stock_continuation(...)`
- `classify_index_gap(...)`
- `opening_relative_volume(...)`

The module returns decisions and price levels; it does not place broker orders.
This version should remain in research/paper mode until separately backtested
on Indian OHLCV, disclosure, sector, breadth, spread and slippage data.

## Database-backed paper variant

Strategy key: `gap_up_volume_confirmed`; version:
`v1-prior-volume-15m-confirmed`. This is the broad-equity paper configuration
selected from the database backtest. It scans the deduplicated NIFTY 500,
Midcap 150 and Smallcap 500 universe after the close.

- Gap is 2–8%, above the previous high, with the same ₹50, prior-extension and
  EMA20 guards as the stock engine.
- At least one of the four completed sessions before the gap must have volume
  at least 1.25× its preceding 20-session median.
- Opening 15-minute relative volume is at least 0.75×; the candle closes above
  the typical-price VWAP proxy and in the top 30% of its range.
- Upper wick is no more than 35%; at least 40% of the gap remains.
- Entry is above the first 15-minute high and may trigger through 12:30 IST.
- Initial risk must be at least 0.75% and below 2%; target is 2R, with a 15:15
  exit for trades that reach neither stop nor target.

The day-end runner records closed `EQ` proxy trades in `strategy_trades`. It
uses only `market_snapshots`, is idempotent, and is explicitly paper-only. It
does not claim a verified news catalyst, true exchange VWAP, point-in-time
index membership, or live fill quality.
