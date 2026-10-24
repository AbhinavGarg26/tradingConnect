# Nifty daily 20/50 monthly calls only — paper implementation

User-selected rules: one lot, ATM calls on bullish EMA crossover, exit calls and
stay in cash on bearish crossover, and total option premium <= INR 30,000 per new lot. Monthly
rollover is enabled in the candidate rules. The account allocation discussed
is INR 100,000. Premium committed is not a INR 500 stop-loss: this version has
no fixed stop and can lose the entire paid premium. Repeated rolls can incur
additional losses. The monthly option execution and rollover are not validated by the long-only index backtest.

## Signal and contract rules

Use completed daily closes, EMA20/EMA50 seeded with first close, same formula
as the daily backtest. Data starts at 2020-01-01 to preserve its EMA history.
At the next session's opening window (09:16 to before 09:30 IST):

- Fresh bullish crossover -> one ATM monthly CE.
- Bearish crossover -> close the owned call and remain in cash. Never buy puts.
- An existing legacy paper put, if any, is exit-only; do not replace it in the same invocation.
- Bullish trend and outside rollover window -> hold the owned call overnight.
- No position and no fresh crossover -> wait. Do not buy retrospectively simply
  because the current trend is bullish or bearish on first activation.
- At two remaining exchange trading sessions before expiry, roll an owned
  position to the next eligible monthly expiry if its trend remains valid.
  A bearish exit takes priority over rollover.

Monthly expiry is determined by NIFTY futures expiries in the broker instrument
master, then matched to NIFTY options with the same expiry. Weekly options and
calendar weekday guesses are not used. Actual lot sizes come from that master.
Near-expiry entries skip to the next eligible monthly contract. ATM selection
uses minimum distance to fresh spot; ties choose the lower strike. All new lots,
including roll/reversal lots, must fit the cap. No fallback to cheaper strikes.

The opening fill is approximate rather than the historical next-open fill.
Paper buys use fresh best ask and sells use fresh best bid, with sufficient
visible quantity for the whole lot. Quotes must be no more than 60 seconds old.
There is no broker order API in the paper runner. It cannot activate live orders.

## Paper journal and performance

Key: nifty_daily_ema_monthly. Version: ema20_50_calls_monthly_v2. Historical two-sided records keep their original version. Mode: paper.
Positions are persisted across daily invocations using the shared strategy
journal, scoped by user and strategy. A PostgreSQL advisory transaction lock
prevents concurrent duplicate processing. Successful daily runs are idempotent;
exits and replacement paper lots are journaled in one transaction. Unresolved,
non-paper, or multiple owned positions block processing. Failed quote/budget
validation rolls back journal changes, including simulated exit legs; resolve
blocked rollover before expiry. Expired recorded options require reconciliation.

Strategy Performance has a dedicated setup/current-side section. Once journal
signals are recorded, it shows bullish/calls or bearish/cash, EMA values and the
completed daily date. Until then it explicitly says it is awaiting paper data.
Its monthly paper win rate and profit factor use simulated OPTION premium P&L,
not Nifty points. Each rollover lot is a separate trade; these statistics do not
represent the win rate of an entire multi-month trend campaign. Charges are not
included. Paper positions never appear as live fills.

## Local setup (not deployed or started by Codex)

In portfolio-tracker, apply the pending Rails migrations. The added migration
allows NULL initial/current stops, since the EMA strategy has no fixed index
stop; it does not invent zero-valued stops. The earlier nullable-target migration
must also be applied. Existing intraday strategies keep recording their stops.

```sh
bundle exec rails db:migrate
```

From kiteConnect, read-only readiness/current-side check:

```sh
venv/bin/python nifty_daily_ema_monthly_paper.py --check --skip-incomplete-days
```

To record simulated opening fills during the permitted regular-session window:

```sh
venv/bin/python nifty_daily_ema_monthly_paper.py --max-premium 30000 --roll-sessions 2 --skip-incomplete-days
```

This is a one-shot PAPER invocation, not a broker activation command. It requires
a valid existing Kite session for read-only profile/quotes, the shared database,
updated schema, and supervision of source-data/quote failures. No timer/service
has been enabled and no live runner is provided here. No real trading is enabled.
The implementation does not change the independent guardian/risk settings for
existing intraday strategies.

Missing minute sessions fail unless explicitly excluded. Exclusion dates are
printed; the latest prior trading day's daily candle must always exist. Older
excluded sessions alter EMA history and should be repaired for final validation.

The 20/50 index results do not validate this monthly-call option implementation.
Option premium histories, rolls, spreads, charges and overnight gaps require
separate testing. No historical index trades are inserted as option performance.
