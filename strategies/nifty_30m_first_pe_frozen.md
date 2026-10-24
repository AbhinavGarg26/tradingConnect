# PE1-V1 — frozen research configuration

Frozen on24 October2026 by user instruction. This is the next research baseline,
not a promotion to live trading. Future experiments must use separate names.

- First eligible signal PE: take the trade. First eligible signal CE: skip the
  entire day, including later PE signals. One confirmed entry maximum per day.
- Opening09:15–09:45 body must be >=20. Initial midpoint band remains body5%.
- Initial stop25 NIFTY points from entry; no profit target or trailing stop.
- Original matching-color >40/80 body-reference updates and reference exits.
- Stops/reference exits use completed1m closes; session exit15:15.
- Backtest fills next-minute open with1 adverse index point per side.
- No CE color filter, buffered reference exit, step trail or candle trail.
- No actual option-premium or rupee-profit claims. Live runner unchanged.

Use the dedicated preset to prevent accidental parameter overrides:

```sh
python3 -m backtest.nifty_30m_first_pe_frozen --from-date 2020-01-01 --to-date 2026-10-23 --summary-only --skip-incomplete-days
```

The preset accepts only data-source/date and report controls. Tuning flags are
rejected. Incomplete-day exclusion is opt-in and all excluded dates are reported.

Reference evidence, end23 October2026: full history645 trades,+1612.50 index
points,profit factor1.13,max closed-trade drawdown1064.85; recent2024 onward291
trades,+1191.85,profit factor1.20,drawdown666.20. Samples exclude13 and1 incomplete
days respectively. These previously inspected historical results are not an
independent holdout. Preserve this version for forward paper evaluation.

## Runner preparation

The local runner supports the explicit `--preset pe1-v1` preset, using the same
selection engine and one-entry journal cap. Journal version is `pe1-v1`;
the strategy key remains `nifty_30m_reference` so unresolved older positions
still block startup. The existing systemd service is unchanged.

The local PE1 adapter now checks the saved risk settings and guardian health
before a live buy, validates the option stop inputs, installs and records a
broker-held protective sell stop-limit after a confirmed fill, and attempts
emergency handling if protection or actual allocation checks fail. The
primary runner and guardian share a PostgreSQL advisory lock for broker exits.
Normal exits reserve the trade, confirm protective-stop cancellation, recheck
position/order ownership and sell once. Confirmed guardian closures clear the
runner's local state; pending guardian exits pause its processing.

The previous missing-integration code block has been replaced by these
fail-closed checks. Disabling live entries does not remove protection from
existing positions. Insufficient-funds paper fallback remains per trade;
uncertain live submissions and real holdings never become paper trades.

The frozen index strategy is unchanged. The additional option-premium rupee
risk limits may close earlier than an index/reference exit, so the execution
layer is not identical to the index-only backtest. The risk-settings page
values control these overlays; no amounts or enable flags were changed.

No live activation, deployment, real broker order, or production integration
test has been performed. Automated tests use simulated broker/database
responses. Existing systemd units still select their existing v3 preset.
