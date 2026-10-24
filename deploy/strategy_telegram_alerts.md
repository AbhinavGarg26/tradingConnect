# Strategy Telegram alerts

Alerts are generated from committed `strategy_events` by the existing minute status observer, separately from trading processes. Supported strategies include daily EMA calls, 15m EMA puts/calls, Breakout v3, Body Reference PE1-V1 and Fixed Profit Filtered. Only actionable entry/exit signals and recorded fill events are sent; routine holds/waits are silent. Alerts show PAPER/LIVE explicitly. A signal alert is not confirmation that an order was placed or filled. The observer normally delivers within its approximately one-minute schedule.

Apply targeted Rails migration 20261024070000 before deploying the observer hook. Telegram credentials use the account's existing configuration; never print them. The explicit account opt-in is stored in `strategy_telegram_subscriptions`. `enabled_at` prevents historical alert replay. `strategy_telegram_deliveries` stores a unique signal identity and sent/failed/uncertain status. Events older than 15 minutes are not sent as new trade signals.

Delivery is claimed before contacting Telegram. A restart never automatically resends a claimed notification. If a timeout or crash makes delivery ambiguous, the record remains uncertain/sending and needs review; Telegram does not support idempotent sends. A failed notification never retries a broker order or changes strategy execution.

The 24 October 2026 installation opted account 975447485 into alerts at the user's request, without enabling trading or changing any trading timer. No historical strategy events or order records were fabricated for testing. Unit tests mock Telegram and verify restart deduplication, scope/age filtering, quiet holds, escaped message content, missing configuration, explicit API rejection and ambiguous delivery.
