# Confirmed option reversals

The `market_breath.py` service runs a reversal worker for Zerodha and linked Groww accounts. Existing stop-loss monitoring continues separately.

Before enabling, apply `resources/create_market_reversal_states.sql` to the same
PostgreSQL database used by the service, with the schema owner. This creates the
durable journal and seeds Market Config rows without replacing existing values.
Restart market_breath after deploying. Set `reversal_enabled=true` in Market
Config to activate. The initial value is false; no live trades are enabled by
this change alone.

Defaults: 5% premium drawdown from the observed post-entry peak; permanent
ineligibility for that lifecycle once its peak gain exceeds 10%; one reversal
attempt per user across both brokers per IST day (maximum configurable 2); 300-second cooldown;
180-second confirmation window; 5% funds reserve; INR 2,000 account daily loss
budget; 15:00 IST entry cutoff. Hard-stop and max-loss settings still apply.
`risk_per_trade` and `max_open_trades` additionally constrain new entries.

Only long NSE F&O options with an unambiguous NSE underlying and opposite contract
are eligible. Groww contracts must match the Kite instrument master exactly. A valid
Groww exchange link/session is required. Each broker also respects its existing
auto-exit enable switch; disabling Kite exits does not disable Groww reversals. Reverse into the same strike, expiry and product. The daily attempt
slot is consumed before submitting an exit, including skipped or failed attempts.
The slot is deliberately not refunded. Config changes reload every two seconds.

1. Persist peak and eligibility. Exclude ignored/overridden symbols and positions
   already beyond a hard loss boundary.
2. At the drawdown threshold, reserve a daily attempt durably. Cancel protective
   GTTs and close through the shared, serialized exit executor.
3. Require the owned exit order to be COMPLETE for the original quantity, and
   verify the broker position is flat. Partial/replaced/ambiguous exits cannot
   open an opposite leg.
4. Require two consecutive completed one-minute underlying candles. The latest
   must start after the trigger, be fresh, and close below the prior low for
   CE-to-PE, or above the prior high for PE-to-CE. No confirmation means stay flat.
5. Check fresh quotes (15 seconds), positive depth and spread at most 2%; size
   whole lots using funds, remaining daily loss budget, per-trade risk and the
   configured stop percentage. Reduce lots as needed, never exceed original
   quantity. Check broker order margins and recheck positions before submission.
6. Persist BUY_SUBMITTING before sending one price-capped BUY. Zerodha uses IOC;
   Groww uses DAY validity and requests cancellation of any unfilled remainder on
   the next reconciliation poll, including partial fills. Cancellation is
   asynchronous: fills can occur before it completes. Reconcile its
   unique tag; never retry an unknown submission or top up a partial fill.
   The existing position monitor protects any actual opposite fill.

`market_reversal_states.state` records peaks, count, pending intent and history.
A per-user PostgreSQL session advisory lock prevents multiple reversal workers
from submitting concurrently. Terminal attempts move to history on the next
cycle. A BUY_SUBMITTING intent with no matching broker order intentionally blocks
further automatic reversals: reconcile the broker order/trades manually before
changing its status to SKIPPED. Never clear an uncertain intent merely to retry.

Peak tracking starts when the feature is enabled; earlier unseen highs cannot be
reconstructed. Stored peaks survive restarts within the trading day. Same-day
buy execution aggregate changes establish a new tracked lifecycle. For reliable
peak tracking, enable before entering eligible positions. The daily loss gate
uses the trading broker’s position P&L: Zerodha net-position P&L, or Groww FNO
realised P&L plus unrealised P&L from reconciled entry executions and Kite LTP.
Loss budgets and funds remain broker-specific; the daily attempt counter and
cooldown are shared across both. Neither is a combined, charge-adjusted ledger. Stop-based sizing is an estimate;
gaps and execution slippage can exceed the budget.

Validation uses mocked broker responses and an in-memory journal adapter; it does
not submit live broker orders or apply the database migration. Paper/replay
validation with historical option data is still needed to assess profitability.

The existing `reversal_enabled` switch activates both eligible brokers. No
additional schema changes are required if the reversal migration is already
installed. The journal's pending intent includes its owning broker; legacy
intents without this field belong to Zerodha. Peaks for Groww are namespaced so
identical contracts in both accounts cannot share peak state. Missing/expired
Groww credentials do not grant Zerodha ownership of a Groww pending intent.

Groww HTTP request shapes follow the official order, margin, portfolio and smart
order documentation:
- https://groww.in/trade-api/docs/curl/orders
- https://groww.in/trade-api/docs/curl/margin
- https://groww.in/trade-api/docs/curl/portfolio
- https://groww.in/trade-api/docs/curl/smart-orders
