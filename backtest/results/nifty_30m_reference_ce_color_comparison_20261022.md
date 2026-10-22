# Frozen V3 versus CE-green V3C

Historical period: July 1–October 21, 2026. Compared using the same cached
29,250 Kite one-minute Nifty candles. Stop25, max2 entries/day, opening body>=20,
opening body buffer5%, reference exit buffer0, and one adverse index point per
side. Baseline is still the default. No production changes or live orders.

V3C changes only this entry gate: completed CE signal candle close > open.
Red and flat CE signal candles are rejected, without consuming an entry. PE
color is unrestricted. First entries can qualify on a later green candle while
price remains beyond the reference. Later re-entries retain V3's fresh-reference
cross requirement; rejected crossings are not queued. Use the completed signal
candle, never the next fill candle's eventual close.

| Metric | Frozen V3 | V3C CE-green |
|---|---:|---:|
| Trades | 93 | 92 |
| Net Nifty index points | +892.70 | +915.10 |
| Win rate | 21.51% | 21.74% |
| Profit factor | 1.72 | 1.76 |
| Maximum closed-trade drawdown | 242.80 | 246.20 |
| Worst trade | -47.95 | -47.95 |
| CE trades / net | 45 / +135.50 | 44 / +157.90 |
| PE trades / net | 48 / +757.20 | 48 / +757.20 |
| Open positions / pending orders | 0 / 0 | 0 / 0 |

| Month | V3 | V3C | Difference |
|---|---:|---:|---:|
| July | +1.25 | -3.35 | -4.60 |
| August | +85.25 | +84.55 | -0.70 |
| September | +136.55 | +133.15 | -3.40 |
| October through21 | +669.65 | +700.75 | +31.10 |
| Total | +892.70 | +915.10 | +22.40 |

All five largest baseline winners retained their original entry and result:

| Date | Side | Entry IST | Points in both |
|---|---|---|---:|
| October21 | PE | 09:51 | +310.75 |
| September15 | PE | 09:46 | +265.15 |
| October12 | PE | 09:46 | +211.00 |
| October15 | CE | 09:46 | +167.50 |
| August4 | PE | 09:46 | +160.85 |

July13's CE winner changed from 09:46 entry / +131.50 to 09:47 entry / +117.45.
October21's CE 09:46 loss of31.10 was avoided; its PE winner was unchanged.
Across July–September, total results worsened8.70 points. Thus the aggregate
22.40 improvement is concentrated in the motivating October21 example.
Higher net and slightly higher profit factor did not reduce drawdown. This
sample does not establish broader improvement; retain frozen V3 as default
and keep V3C experimental pending untouched-date validation.

Reproduce comparison:

```sh
python3 -m backtest.nifty_30m_reference --fetch --from-date 2026-07-01 --to-date 2026-10-21 --stop-points 25 --max-entries 2 --slippage-points 1 --compare-ce-color --summary-only
```

Use `--ce-color-filter` for V3C alone. Do not combine this with the reference-exit
buffer experiment; the runner rejects that combination to isolate the test.
Returns are index-point proxies, excluding actual option premiums, fees/taxes.
