# Nifty 30m Body Reference v3

Separate engine: `Nifty30mReference` in `strategies/nifty_30m_reference.py`.
Identity `nifty_30m_reference`, version v3. Earlier strategies are unchanged.
No EMA, doji filter, profit target, or early-doji stop applies.

## V3 opening eligibility and daily cap

Before allowing any entry, check the first 30-minute candle's body:
body=abs(close-open). Allow the session only when body >=20 Nifty points.
A body below20 rejects the day regardless of its high-low range or wick size.
The former wide-range exception is removed. Keep the original midpoint/body
entry band. Later large candles cannot enable a day rejected by the opening
filter. Eligibility resets and is recalculated next session.

Default maximum is three confirmed entries combined across CE and PE. Exits
remain enabled after the third entry. The initial stop remains20 points;
compare20/25/30 under these new rules before selecting a replacement.

## Opening entry and initial protection

From completed 1m data, aggregate the first 09:15–09:45 IST candle. Set
M=(O+C)/2, B=abs(C-O)*5%. Initial CE reference=M+B; PE reference=M-B.
The first completed minute after 09:45 with close above the CE reference buys
CE, or below the PE reference buys PE. Earliest signal 09:46; simulated fill
at the following minute's open. Equality does not enter. Entry decisions use
the Nifty index; select current-expiry ATM options separately at each entry.

Initial stop defaults to 20 Nifty points from the confirmed entry index quote:
CE entry-20; PE entry+20. It stays fixed for the trade. The body-reference exit
also stays active, so a trade may exit before reaching the 20-point stop.
These are index triggers, not premium stops.

## Subsequent reference candles

Thirty-minute buckets align to 09:15, 09:45, 10:15, etc. Only a fully completed
bucket can update a reference. High/low wick size does not determine eligibility:
body=abs(close-open). Ignore body <=40 points. Exactly 80 uses the open rule.
The first opening candle supplies only the initial midpoint band.

While holding PE, consider only red candles; while holding CE, only green:

| Body size | PE red candle | CE green candle |
|---|---|---|
| >40 and <=80 | open +5 | open -5 |
| >80 | (open+close)/2 +10 | (open+close)/2 -10 |

Updates tighten only: PE reference=min(old,candidate); CE=max(old,candidate).
This is an explicit implementation assumption. Ignore opposite-color, small,
and risk-widening reference candidates. References do not update while flat;
retain each side's latest reference for potential re-entry. The other side
retains its own initial/reference level. Once a qualifying bucket completes,
its new level applies to that close and future minute closes, never earlier.

## Minute exits and re-entry

Evaluate every completed minute. PE exits at close >= its reference or >= its
fixed stop. CE exits at close <= its reference or <= its stop. Square off at
15:15 regardless of references and disallow subsequent entries. Priority:
session end, fixed stop, reference exit. Stop/ref triggers are candle-close
checks; intrabar touches alone do not exit. Fills can cross the stop level.

After the first confirmed entry, all later entries require a fresh crossing
back into the qualifying region of the applicable retained reference: PE
previous close>=reference and current close<reference; CE reverse. Exits and
entries cannot share a signal candle. No special early-session-only logic:
reference exits and re-entries apply throughout the session.

Maximum three confirmed entries combined across CE and PE per day. Exits do
not consume entries. Rejected/unfilled buys do not consume entries. Reset only
at the next session after all positions/orders are reconciled.

## Backtest

```sh
python3 -m backtest.nifty_30m_reference --fetch --from-date 2026-07-01 --to-date 2026-10-21 --stop-points 20 --max-entries 3 --slippage-points 1 --summary-only
```

Compare predefined stop sizes using the same one-minute history:

```sh
python3 -m backtest.nifty_30m_reference --fetch --from-date 2026-07-01 --to-date 2026-10-21 --stop-points 20 --compare-stops 10,15,20,25,30 --max-entries 3 --slippage-points 1 --summary-only
```

Or replace fetch/dates with a local one-minute CSV path. Columns:
`date,open,high,low,close`; dates represent interval starts. Naive dates mean IST.
Consecutive bars from 09:15 are required; include 15:15 candle open for exit fill.
Default slippage is zero; examples stress each side by one adverse index point.
Completed-candle signals fill at the next minute open. The summary reuses v3
statistics; R uses the initial fixed stop distance, not the dynamic reference.
Open positions and pending final signals are reported, not forcibly closed;
unresolved trades cannot silently cross sessions. Without --summary-only,
print signals, individual trades and reference changes.

A stop comparison measures the chosen sample, not a proven best stop. Validate
chosen settings on untouched dates. Results are Nifty index-point proxies,
excluding actual option premiums, bid/ask spread, fees and taxes.

## Live integration

Feed completed `Bar` instances to `on_bar`. Signal delivery does not assume a
fill. Call `confirm_fill(index_price=actual_nifty_at_entry)` after a buy fill;
call `confirm_fill()` after an exit fill. Use `reject_signal()` only after
confirming no fill. Persist positions, references, fixed stop, entry count and
aggregation state; reconcile before restarting. Current-expiry ATM selection
can reuse `current_atm_option` from `strategies.nifty_opening_range`; keep the
selected contract until exit and read its lot size from the broker catalog.
The frozen V3 live runner is now `nifty_30m_reference_live.py`. See `deploy/nifty_30m_reference_live.md` for deployed scheduling, option selection, journal, funds fallback, and operational details.

## Frozen V3 and isolated reference-buffer experiment

V3's rules and default reference-exit buffer remain zero. Its known comparison
configuration is `--stop-points 25 --max-entries 3 --slippage-points 1` (the
constructor's original stop default remains20; pass25 explicitly). On July1
through October21, 2026 this reproduced126 trades, +977.55 index points and
274.35 closed-trade drawdown.

Experimental `Nifty30mReferenceBuffered` lives in the separate
`strategies/nifty_30m_reference_buffered.py` module. It overrides only the exit
threshold: CE reference-5; PE reference+5. The entry band, fresh-cross re-entry,
reference updates, fixed stop and session cutoff are unchanged. Stop takes
precedence if price crosses both levels. Do not use buffered exit levels for
entry checks or reference updates.

```sh
python3 -m backtest.nifty_30m_reference --fetch --from-date 2026-07-01 --to-date 2026-10-21 --stop-points 25 --max-entries 3 --slippage-points 1 --compare-reference-buffer --summary-only
```

`--reference-exit-buffer 5` runs V3B alone; the default0 runs V3. The controlled
comparison always reruns both0 and5 on the same in-memory data and parameters.
It reports net points, win rate, profit factor, drawdown, worst trade, exit
breakdowns, monthly results and unresolved positions. This is an experiment;
V3B is not promoted automatically based on performance.

## Isolated CE-green experiment: V3C

Frozen V3 remains the default. `Nifty30mReferenceCEGreen` in the separate
`strategies/nifty_30m_reference_ce_green.py` adds only a CE entry gate:
completed signal close > open. Flat/red signal candles cannot buy CE. PE entry
rules are unchanged. Rejecting a signal does not consume a daily entry. Entry
and exit references, fixed stop and fill assumptions are unchanged. Never use
the future fill candle's close to decide whether to enter.

A rejected first CE attempt can qualify on a later green minute still above
the entry reference. Re-entries retain V3's fresh reference-cross requirement;
this experiment does not introduce pending crossing setups. Changing CE
position availability can change later opportunities, including PE trades,
even though the PE rules themselves do not change.

```sh
python3 -m backtest.nifty_30m_reference --fetch --from-date 2026-07-01 --to-date 2026-10-21 --stop-points 25 --max-entries 2 --slippage-points 1 --compare-ce-color --summary-only
```

`--ce-color-filter` runs V3C alone. Without that switch the runner uses V3.
The comparison reruns both on the same rows and reports P&L, drawdown, monthly
and CE/PE results, plus same-entry retention and overlapping replacements for
the five largest baseline winners. It also lists all missing baseline CE
winner entries for manual review. Same-entry absence does not imply the whole
move was missed; inspect delayed replacements. The CE-color and reference-exit
buffer experiments cannot be combined in this runner.

## Isolated step-trailing experiment: V3D

`Nifty30mReferenceStepTrail` in `strategies/nifty_30m_reference_step_trail.py`
adds 10-point completed-close profit steps. CE: favorable move10 sets stop to
entry, move20 sets entry+10, move30 sets entry+20, etc. PE uses entry, entry-10,
entry-20. Price may jump multiple levels; use the highest favorable step reached
and never loosen. Stops use the confirmed index entry reference, not an option
premium. Every new entry resets trailing state. Reference exits and session
cutoff are not delayed. Initial25-point protection remains until the first step.
Intrabar high/low touches do not arm or ratchet the close-based trail.

```sh
python3 -m backtest.nifty_30m_reference --fetch --from-date 2026-07-01 --to-date 2026-10-21 --stop-points 25 --max-entries 3 --slippage-points 1 --compare-step-trail --summary-only
```

Default `--trail-step-points 0` preserves V3. Select10 for V3D alone. The
comparison runs0 and10 on identical data and reports monthly results, win rate,
profit factor, drawdown, worst loss, trail exits and matched-entry outcomes of
the five largest V3 winners. Step trailing cannot combine with the other
experiments in this runner. Exit reason `step_trail_stop` distinguishes activated
trailing protection from the original `fixed_stop`. Initial R always uses the
original risk, not a later breakeven stop.

A breakeven or positive index trigger does not guarantee a profitable exit:
close-based monitoring, next-open fills, slippage and actual option-price
behavior can all differ. Same-sample tests are not validation on untouched data.
