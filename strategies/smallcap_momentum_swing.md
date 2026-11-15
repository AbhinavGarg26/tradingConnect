# Smallcap Momentum Swing v2

Identity: `smallcap_momentum_swing`, version `v2-first-15m-strength-confirmation`.
This is a broker-agnostic research strategy based on the three linked Manas
Arora swing-trading videos. It does not place live orders.

## Rules

1. **Find leaders:** Flag a stock when its close exceeds the prior 20-session
   high and volume is at least 2× its prior 20-session average. Also accept a
   gain of at least 5% on volume at least 2.5× average. Add it to a focus list
   and keep following it rather than rescanning into a new name each day.
2. **Wait for a base:** Require at least a 30% impulse from the 60-session low.
   The current pullback from the 60-session high must be no deeper than 25%.
   Wait for range contraction (last range no more than 4.5% and 65% of the
   preceding ten-session median), three higher lows, and a close in the upper
   half of the day's range.
3. **Enter only on a strong start:** On the next session, require a gap above
   the setup day's high, a hold above the previous day's low, a break and
   15-minute close above the prior high, a close in the upper 30% of the
   first 15-minute range, and opening volume at least 1.5× the same-time historical
   baseline. The initial stop is the low of the opening move. The exact
   entry/stop check is implemented by `confirm_strong_start`; the caller must
   supply completed opening bars and same-time relative volume.
4. **Manage the position:** After an 8% gain from entry, raise the stop to at
   least breakeven. Trail a young move with the 20-day average; once the move
   from its base reaches 40%, trail with the 10-day average. Around a 50% move
   from the base, scale out 50–75%. Exit the rest on the protective stop or
   around a 70% move from the base. Each re-entry should have its own stop.

The videos describe the setup visually and leave judgment to the trader. The
numeric values above are explicit starting heuristics for repeatable research,
not claimed as exact thresholds quoted from the videos.

The v2 opening-close rule is supported by a deterministic 560-trade cross-year
sample from the strategy's baseline signals. It reduced entry-session stops
from 34.96% in rejected signals to 12.26% in confirmed signals. Requiring the
first 15-minute close in the upper 30% with relative volume at least 1.5×
produced a 1.818% average baseline-trade return and a 2.20 profit factor across
development, validation, and recent test splits. This is a sampled filter study,
not yet a full intraday execution simulation.

## Backtest

Run against current NIFTY Smallcap 500 constituents. The runner uses the same
read-through flow as the broad RSI backtest: it syncs current equity instruments,
reads daily OHLCV from `market_snapshots`, fetches missing completed sessions
from Kite, and saves those candles back to the database for later reruns:

```bash
venv/bin/python -m backtest.smallcap_momentum_swing
```

To forbid Kite fetching and use only previously cached rows:

```bash
venv/bin/python -m backtest.smallcap_momentum_swing --database-only --skip-instrument-sync
```

The backtest is deliberately labelled a **daily-bar proxy**. It approximates a
strong start with a next-session open above the setup high and cannot verify
the first 5–15-minute hold or same-time volume condition. It uses the setup-day
low as a known initial stop, which also differs from the video’s opening-move
low. Current index membership introduces survivorship bias. Results exclude
taxes and market impact, and do not model account-level position sizing. The
report writes to `outputs/smallcap_momentum_swing_backtest.json`.

## Source videos

- [The Only Swing Trading Guide You’ll Ever Need](https://www.youtube.com/watch?v=WCYhKrMzG50)
- [Swing Trading Case Study: Entry, Re-Entry and Exit Explained](https://www.youtube.com/watch?v=Va-ezKU64vY)
- [Swing Trading: Why You Exit Winning Trades Too Early](https://www.youtube.com/watch?v=0Gtv5YVBfcQ)
