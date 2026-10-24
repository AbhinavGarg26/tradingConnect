# Frozen 15m 9/21 bearish strategy — preparation handoff

Key: nifty_bearish_15m_9_21. Version: bearish_15m_9_21_v1.
Status: signal implementation prepared; execution disabled; no timer installed.

The immutable research reference is
strategies/nifty_bearish_15m_9_21_frozen.json. Its rule/configuration and source
checksums are not changed. The packaged Bearish15m engine reproduces the saved
backtest signal sequence exactly over the entire evaluation period.

## Signal specification

On complete session-aligned 15-minute Nifty candles, EMA9/EMA21 use alpha
2/(N+1), seeded with the first source close. Warmup/history starts 2020-01-01.
Bearish entry: previous EMA9 >= EMA21 and current EMA9 < EMA21. Exit: the inverse
cross. Observe at candle close; the backtest fills at next candle open. Stay in
cash after exit, with no retrospective entry merely because EMAs are bearish.
Hold overnight. Do not add a stop, target, same-day square-off or new filter to
this frozen signal version. Any such change needs a separately tested version.

The preparation CLI is READ ONLY with respect to the account and strategy
journal. It writes only a local readiness artifact, never submits orders, never
enables a service, and does not represent quote-based option paper trading.

```sh
venv/bin/python nifty_bearish_15m_prepare.py
```

It verifies frozen source/trade hashes, replays completed source candles and
writes outputs/nifty_bearish_15m_preparation/readiness.json. The default source
is a historical database export, not a live feed. Historical/off-session signals
are reported but cannot be promoted to a current entry. This command does not
establish ownership of any broker position or carry an overnight pending order.

## Candidate option execution profile

Using the previously discussed option constraints: one ATM monthly PE lot,
premium at most INR 30,000, actual lot size from dated instrument metadata,
monthly expiry identified from listed monthly futures, rollover two exchange
sessions before expiry while the bearish trend remains valid. Cash after the
bullish exit. The profile is explicitly disabled, validated as puts-only, and
separate from the frozen index-signal rules. These option constraints have NOT
been validated with historical premium data. The premium cap is not a fixed
stop-loss; the full premium may be lost. No real execution adapter is provided
or enabled by this preparation step.

## App registration

Source changes in portfolio-tracker register a separate strategy-list entry
and a preparation section in Strategy Performance. The UI marks it unscheduled,
with no live activation control. It will not mix its version or metrics with
the daily calls-only strategy or insert historical index signals as option P&L.
An eventual option-paper journal uses the dedicated key/version, option premium
P&L, and BEARISH_EMA_SIGNAL events for the current-side display. That quote-based
journal/feed integration is still pending. No app deployment was performed.

## Remaining execution work

Before any user-controlled live deployment, separately review the live feed and
closed-bar finalisation, persistence of pending next-bar and overnight signals,
trading-day/expiry calendar, fresh bid/ask and depth validation, budget and funds
checks, strategy-specific ownership, cross-strategy contract conflicts, partial
fills, idempotent durable order intents, restart recovery and broker reconciliation.
An exit/roll must not accidentally sell a manual lot or short an option. Rollover
must reconcile its close leg before buying the replacement. Quote staleness,
missed bars, ambiguous orders and expired contracts must halt and report clearly.

A separate protection/overnight risk policy and historical option-premium
validation remain outstanding. The existing index backtest (680 closed legs,
32.06% wins, PF 1.2449) does not establish put option profitability. No claim of
live readiness, option-paper fill coverage or high win rate is made here.

Codex did not activate or schedule real securities trading. User-managed
broker execution, reviewed infrastructure and manual activation are separate
from these local source changes.
