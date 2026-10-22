# Frozen V3 versus V3D 10-point step trail

Same cached 29,250 Nifty one-minute candles, July1–October21, 2026. Initial
stop25, max3 entries/day, body-only opening filter>=20, no CE-color filter,
no reference-exit buffer, one adverse index point slippage per side.

V3D uses favorable completed-close movement from the confirmed index entry:
+10 => stop at entry, +20 => stop locks10, +30 => locks20, continuing by tens.
PE is symmetric. Stops only improve, can skip multiple levels on one close,
and reset on each new confirmed entry. Intrabar highs/lows do not ratchet the
close-based stop. Fixed protection before activation, reference exits and
15:15 cutoff remain active. Exit fills occur at the following open; breakeven
arming is not a guarantee of no loss. Initial-risk/R metrics retain25 points.

| Metric | Frozen V3 | V3D |
|---|---:|---:|
| Trades | 126 | 124 |
| Net index points | +977.55 | +120.15 |
| Win rate | 19.05% | 26.61% |
| Profit factor | 1.65 | 1.13 |
| Max closed-trade drawdown | 274.35 | 232.95 |
| Worst trade | -47.95 | -35.60 |
| Step-trail exits | 0 | 60 |
| Step-trail exit net | 0.00 | +944.75 |
| Open / pending positions | 0 / 0 | 0 / 0 |

| Month | V3 | V3D |
|---|---:|---:|
| July | -43.70 | -84.00 |
| August | +16.30 | -1.05 |
| September | +362.15 | +42.50 |
| October through21 | +642.80 | +162.70 |

| Baseline winner | Side | Original points | Same-entry V3D points |
|---|---|---:|---:|
| October21 09:51 | PE | +310.75 | +14.60 |
| September15 09:46 | PE | +265.15 | +42.80 |
| October12 09:46 | PE | +211.00 | +25.55 |
| September11 10:02 | CE | +171.35 | +23.45 |
| October15 09:46 | CE | +167.50 | +116.75 |

Net deteriorates857.40 points while drawdown improves41.40. All four monthly
periods worsen. More winning trades do not offset the reduction in large
winners. Retain frozen V3 as default; V3D remains an explicitly selected
experiment. Results are same-sample index proxies, not actual option P&L.

Reproduce:

```sh
python3 -m backtest.nifty_30m_reference --fetch --from-date 2026-07-01 --to-date 2026-10-21 --stop-points 25 --max-entries 3 --slippage-points 1 --compare-step-trail --summary-only
```

Use `--trail-step-points 10` to run V3D alone. Default0 leaves V3 unchanged.
