# Stock inbox and portfolio Telegram summaries

This change spans tradingConnect and portfolio_tracker. Deploy the Rails migration
`20261117090000` before restarting Python workers with the new sender. It creates
`stock_notifications` and `portfolio_summary_deliveries`; do not enable the sender
against an unmigrated database.

## Behaviour

- The scanner's volume, daily relative-volume, price-movement and support/resistance/
  EMA proximity notifications now persist to the inbox. Support rejection also
  uses the inbox. These alerts never fall back to Telegram on an insert failure.
- Inbox records and proximity cooldown updates share the scanner transaction.
  Failed inbox inserts use a savepoint and do not advance proximity cooldowns.
- The authenticated `/stock_notifications` page shows account-specific alerts,
  filters, read state and paginated stock history. Its stock page compares the
  latest captured price with support/resistance and EMA 65/100/200, shows capture
  timestamps, and charts captured alert prices (not continuous candle history).
- Existing operational order/stop/session/connection and account-risk alerts remain
  on their existing Telegram path. Strategy Telegram subscriptions are unchanged.
- The existing market-session monitor schedules portfolio summaries at **10:00,
  11:00, 13:00 and 15:00 IST**, on NSE trading days. The latest due checkpoint is
  considered; missed earlier checkpoints are not replayed in a burst.
- Reports show Kite, Groww and combined realised/open/session P&L. Kite funds and
  available balance are included. Groww totals require both CASH/FNO responses,
  reported realised figures, current Kite quotes and reconciled execution entry
  prices. An overnight or unreconciled position, missing quote or partial API
  failure explicitly makes Groww/combined totals unavailable, never zero.
- Report generation is in a separate thread with independent DB/broker sessions,
  so Groww calls do not block the position-monitor loop. No order endpoints are used.

## Delivery guarantees

A unique `(user_id, session_date, checkpoint)` claim commits before Telegram is
contacted. Confirmed responses become `sent`, explicit rejections `failed`, and
ambiguous network outcomes `uncertain`. A crash can leave `sending`. None of those
claims is automatically replayed. Telegram offers no idempotent send API: this
favours avoiding duplicate reports over silently retrying an ambiguous send.
Inspect these rows if a report is missing; do not delete uncertain/sending claims
without first checking whether Telegram received the message.

The monitor must be running through the session to emit all four reports. Existing
NSE holiday/session checks apply. Telegram credentials remain account-scoped via
the existing configuration. Tests use mocked broker/Telegram calls; no live
notifications or orders are sent during verification.

## Rollout

1. Deploy the companion Rails change and run:
   `RAILS_ENV=production bundle exec rails db:migrate:up VERSION=20261117090000`
2. Deploy this engine change, then restart the existing market-fetcher and
   market-breath services using the normal deployment process.
3. Open `/stock_notifications` and inspect a real new alert; confirm its stock link
   shows only the current account's history and support levels.
4. At the next checkpoint inspect `portfolio_summary_deliveries` for the current
   account/date. Confirm the report in Telegram and inspect any incomplete Groww
   data before relying on a combined total.

Apply the migration before Python, and roll Python back before removing either
table. No historical Telegram messages are imported into the inbox.
