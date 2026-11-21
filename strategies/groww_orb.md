# Groww ORB research candidate

The two source videos are [spot study](https://www.youtube.com/watch?v=A0PszCOfm5s)
and [options study](https://www.youtube.com/watch?v=-vVYU3-ZmFs).
This is a separate research strategy because the deployed `nifty_15m_breakout` v3
breaks the *previous rolling 15-minute candle*, while `nifty_30m_reference`
PE1-V1 uses a 30-minute *body midpoint*. Neither trades the fixed opening
high/low rules below. Existing live deployments remain untouched.

`backtest.groww_orb` implements three independently scored setups using NIFTY
one-minute opening-time candles (09:15–15:29 IST), one trade per setup per day:

| Setup | Opening range | Entry on completed 1m close | Filter |
| --- | --- | --- | --- |
| breakout | first 60m high/low | first close outside range | width/open >0.5%; signal before 12:00 |
| retest | first 30m high/low | first outside close, then inside close, then outside close in same direction | width/open <=0.5%; second breakout within 60m of first |
| reversal | first 5m high/low | first outside close, then inside close, then opposite outside close | reversal signal before 12:00 |

The direction is CE above the range and PE below it. Fill the signal at the
following minute's open; stop on a subsequent one-minute close beyond the
opposite opening boundary and fill at the next open. Otherwise exit at the
15:29 candle close. There is no profit target. An adverse index-point slippage
parameter applies independently to entry and exit. A gap through the initial
stop rejects entry. Complete minute data is mandatory; no filling of gaps.

```sh
venv/bin/python -m backtest.groww_orb --database --from-date 2025-01-01 --to-date 2026-11-20 --slippage-points 1 --output outputs/groww_orb_trades.csv
```

The `--database` flag uses only persisted snapshots; missing days fail. A CSV
with `date,open,high,low,close` can replace it. The output is a NIFTY index
proxy. It does not reproduce the video's published returns without its exact
historical option ticks and fill assumptions.

Part 2 examines ATM CE/PE buying versus 0.5/0.2 delta credit and debit
spreads. It reports that option buying performed best 3–4 calendar days before
expiry for breakout, expiry day and one day before for retest, and 1 or 3 days
before for reversal. It also reports weak results two days before expiry on
retest/reversal. These are historical subdivisions, not live filters; expiry
weekday and contract availability changed through the sample. The videos use
65 units for comparison and omit fees and slippage. Before any paper option
runner, align contract expiry to the historical trading calendar, obtain actual
option candles for the selected strikes, apply executable fills and charges,
and evaluate an untouched period. No broker orders are issued by this module.
