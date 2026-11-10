# NIFTY noise-area momentum v1 — paper only

This is a NIFTY adaptation of the time-of-day noise-area momentum method
described by Zarattini, Barbon, and Aziz. It replaces the rejected EMA scalper;
it is not an incremental parameter adjustment to that system.

For every 30-minute decision time, calculate the absolute NIFTY movement from
the session open at that same time over each of the preceding 14 complete
sessions. The average movement defines a clock-specific noise boundary. Adjust
the upper and lower reference anchors for the gap from the previous close.

- Above the upper boundary: bullish index signal (buy CE in a future runner).
- Below the lower boundary: bearish index signal (buy PE in a future runner).
- Inside the boundary: no position, or exit the current position.
- Observe only completed half-hour intervals; fill no earlier than the next
  minute open.
- First decision 09:45, last new entry 14:45, mandatory exit at 15:15 IST.
- No averaging down, martingale, overnight exposure, or opposite-option hedge.

## Evidence and decision

The backtest uses 1,667 complete sessions from 2020-01-01 to 2026-10-23. It
charges two index points per completed trade plus 0.5 point adverse slippage on
each entry and exit. VM=1 was selected only on 2020-2023; 2024-2025 and 2026
were not used for selection.

- Development: +3,540.75 points, PF 1.216, 740 trades.
- Validation: +2,924.75 points, PF 1.267, 402 trades.
- Holdout: +1,132.20 points, PF 1.273, 145 trades.
- Full sample: +7,597.70 points, PF 1.241, drawdown 1,081.80 points.
- Six of seven calendar years were positive; 2023 lost 372.60 points.
- The full sample remains positive with six total friction points per trade.

Status is **live-capable but default-disabled**. This is an index-point proxy, not historical option
premium P&L. The index source contains no genuine volume, so the paper's VWAP
exit is intentionally not fabricated. Before live use, replay with timestamped
option bid/ask data, actual brokerage/taxes, lot-size changes, expiry handling,
rejected orders, and capital-based sizing. Require forward paper performance.

The runner is `nifty_noise_area_live.py`. It defaults to paper. Explicit live
mode additionally requires enabled `strategy_risk_settings`, sufficient
allocation for one full exchange lot, a fresh independent guardian heartbeat,
a broker-held option stop-limit, durable database journaling, and confirmed
broker fills. An uncertain order or protection failure stops the session and
requires reconciliation; protection failure after a confirmed buy triggers an
immediate market-exit attempt. The scheduled service remains disabled until the
server operator installs and enables it.
