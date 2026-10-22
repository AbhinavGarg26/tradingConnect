# Frozen V3 versus experimental V3B

Run on October 22, 2026. Fetched 29,250 one-minute Nifty candles for July 1
through October 21, 2026 using the production-stored broker session through a
read-only database connection. No orders submitted or production data modified.

Identical settings: 25-point initial stop, three entries/day, opening body >=20,
5% opening-body band, matching-color >40-point reference updates, one-minute
close triggers, next-open fills and one adverse index point slippage per side.
V3B changes only reference exits: CE reference minus5; PE reference plus5.
Entries and their reference levels are unchanged. Subsequent trade sequences
can differ because exit timing changes position availability and daily count.

| Metric | Frozen V3 | Experimental V3B |
|---|---:|---:|
| Trades | 126 | 115 |
| Net index points | +977.55 | +920.30 |
| Win rate | 19.05% | 27.83% |
| Profit factor | 1.65 | 1.56 |
| Max closed-trade drawdown | 274.35 | 274.75 |
| Worst trade | -47.95 | -47.95 |
| Reference exits | 79 | 58 |
| Reference-exit net | -753.45 | -726.20 |
| Fixed-stop exits | 24 | 27 |
| Fixed-stop net | -734.40 | -826.35 |
| Open positions / pending signals | 0 / 0 | 0 / 0 |

| Month | V3 points | V3B points |
|---|---:|---:|
| July | -43.70 | +71.65 |
| August | +16.30 | -68.65 |
| September | +362.15 | +211.00 |
| October through21 | +642.80 | +706.30 |

V3B loses57.25 points versus V3. Reference-exit losses improve27.25, but fixed
stop losses worsen91.95; the remaining7.45 improvement comes from session-end
results. The higher win rate does not translate to greater total profit or
less drawdown. Retain frozen V3 as default; V3B remains experimental.
This is a same-sample comparison of index proxies, not a future-performance
claim or actual option-premium/rupee P&L; option prices, fees/taxes excluded.

Reproduce:

```sh
python3 -m backtest.nifty_30m_reference --fetch --from-date 2026-07-01 --to-date 2026-10-21 --stop-points 25 --max-entries 3 --slippage-points 1 --compare-reference-buffer --summary-only
```

Run V3B alone by adding `--reference-exit-buffer 5`; zero is the V3 default.
