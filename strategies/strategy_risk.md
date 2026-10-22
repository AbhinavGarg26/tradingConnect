# Shared live-strategy protection

The Rails `/strategy_risk_settings` page stores one risk policy per user and
`strategy_key` in `strategy_risk_settings`. An unsaved or disabled policy blocks
new live entries. For NIFTY 15m, `check_new_entry` verifies that the separate
guardian has a fresh heartbeat, the quoted one-lot option cost fits the
allocation with a 10% pre-trade cushion, and realized daily P&L has not hit
the configured limit. The confirmed fill is checked against the allocation
again. The policy does not guarantee a fill price.

The NIFTY runner puts a broker-held **sell SL (stop-loss limit)** order against
each confirmed long option lot, priced from its actual option premium and
rupee risk. It does not use SL-M because Zerodha does not support SL-M for
index options. Triggering an SL does not guarantee it fills. The guardian
polls independently, records a fresh heartbeat, and attempts an emergency
market-protected exit if the option bid implies the per-trade or aggregate
daily loss limit, the broker stop is missing/rejected/unfilled, or the option
quote is unavailable. Broker/API outages and rejected exits still require
manual action; the page shows a degraded guardian or `reconcile` trade.
The rupee checks use gross option-premium P&L and exclude brokerage, taxes,
and any unfilled portion of an emergency order. A hard action threshold is
not a guaranteed maximum realized loss.

For another strategy to use these safeguards, it must journal live positions
in `strategy_trades` with its own `strategy_key`, call
`trading.strategy_risk.check_new_entry` before buying, record its broker stop
in `protection_order_id`, and reserve exits by changing `status` from `open`
under a row lock before selling. The generic guardian monitors all enabled
strategy keys. Merely saving settings does **not** protect a strategy that
does not follow this integration contract.

Deployment order: migrate `portfolio-tracker` first, deploy `tradingConnect`
second, enable the guardian timer/service, verify its healthy heartbeat, then
save valid caps and enable live entries on the settings page. Existing
NIFTY live trades should be reconciled before deploying because the strategy
runner cannot reconstruct a same-day in-memory position on restart.
