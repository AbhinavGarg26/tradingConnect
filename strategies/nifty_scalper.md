# NIFTY 5-minute scalper v1 (research only; disabled)

## Current decision

Do not activate this strategy in paper or live execution. On 1,667 complete
sessions from 2020-01-01 through 2026-10-23, the baseline lost 8,380.09 NIFTY
points after a two-point round-trip cost. A constrained walk-forward search
reduced trading substantially, but its development leader still lost 141.29
points in 2020-2023 and 519.11 points in untouched 2024-2025 validation. Its
positive 2026 result is therefore not evidence of a stable edge.

The research runner emits `production_action: KEEP_DISABLED` unless one frozen
configuration is positive with profit factor at least 1.05 in development,
validation, and holdout, satisfies minimum sample sizes, and keeps total
cost-adjusted drawdown below 500 index points.

This is a completed-candle signal engine, not permission to place live orders.
It uses NIFTY index bars. At entry, `nearest_weekly_atm_for_signal` selects the
nearest listed unexpired NIFTY expiry and nearest ATM strike: `BUY_CE` selects
CE and `BUY_PE` selects PE. It fails closed if that side is unavailable. Fills,
rupee sizing, broker protection, journaling, and reconciliation remain
delegated to a future reviewed runner.

Rules:

- Five-minute bars only; decisions occur after the bar closes.
- EMA 5/13 establishes direction.
- The previous bar must touch EMA 5, then the current directional candle must
  close beyond the previous high (CE) or low (PE).
- Entries are allowed from 10:15 through 14:30 IST.
- Stop distance is ATR(10), bounded to 8–18 NIFTY points.
- Target is 1.5R; stop moves to entry after 1R.
- Force exit at 15:15 IST.
- Maximum three entries, two consecutive losses, or 30 negative index points
  per session; a 15-minute cooldown follows each exit.

Backtests are index-point proxies. They do not establish executable option
returns because premium history, spread, slippage, brokerage, taxes, and fill
probability are not reconstructed.
