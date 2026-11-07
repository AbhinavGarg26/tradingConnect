# RSI Highway v2 — rising-weekly, close-confirmed swing strategy

Identity: `rsi_highway`, version `v2-rising-weekly-confirmation`. This module is a
broker-agnostic research/paper engine; it does not place orders.

## Frozen rules

- Long equities only. Use completed adjusted OHLC candles.
- RSI is Wilder RSI(14).
- The latest completed monthly RSI and weekly RSI must both be strictly above
  60. RSI above 70 remains eligible.
- The latest completed weekly RSI must be higher than the preceding completed
  weekly RSI. This blocks entries when medium-term momentum is rolling over.
- Market breadth must be supplied as healthy by the universe/breadth service.
  A missing or unhealthy breadth reading blocks new entries and additions.
- A completed daily candle qualifies when RSI crosses from below 50 to at least
  50, or the prior RSI was 50–52 and the current RSI turns upward.
- The qualifying daily candle must close with RSI of at least 52, avoiding weak
  moves that only marginally reclaim the RSI 50 line.
- The qualifying candle must also be a bullish engulfing candle, bullish hammer,
  or bullish close above the previous day's high.
- Signals are generated after the daily close. A caller may execute at the next
  session's open; the engine never treats an unfinished daily candle as final.
- Add only on another complete entry setup, with no more than three total entries
  in the active position.
- Exit when the completed daily RSI is below 50 or the daily close is below the
  stored swing/pattern stop. The five-day swing stop can tighten but never loosen.
- Monthly/weekly misalignment and weak breadth prevent new risk; they do not by
  themselves force an existing position's exit.

Universe selection, corporate-action adjustments, liquidity, position sizing,
fees, slippage, order persistence and breadth calculation must be supplied by a
caller before any forward paper evaluation. Historical validation is required
before considering live integration.

The backtester uses `market_snapshots` / `1d` as a read-through candle cache.
It fetches missing completed sessions from Kite and saves them idempotently, so
subsequent runs reuse the stored database rows.
