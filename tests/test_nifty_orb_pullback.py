import unittest
from datetime import datetime, timedelta

from backtest.nifty_orb_pullback import Config, aggregate_minutes, compare_variants, exit_fill, run, summarize


def candles():
    rows = []
    for day in range(4):
        start = datetime(2026, 10, 19+day, 9, 15)
        for i in range(75):
            price = 100 + day*10 + i*.05
            rows.append(dict(date=(start+timedelta(minutes=5*i)).isoformat(),
                             open=price, high=price+2, low=price-2, close=price+.1))
    return rows


def setup_rows(short=False):
    rows = candles()
    # Last day's range high is 132.25. Breakout and first pullback follow.
    bars = [
        (132, 134, 131, 133.8),
        (132.5, 134, 132, 133.8),
        (134, 135, 133.5, 134.8),
        (135, 145, 134, 144),
    ]
    for i, prices in enumerate(bars, start=6):
        rows[225+i].update(zip(('open', 'high', 'low', 'close'), prices))
    if short:
        for b in rows:
            o, h, l, c = (b[k] for k in ('open', 'high', 'low', 'close'))
            b.update(open=300-o, high=300-l, low=300-h, close=300-c)
    return rows


class PullbackTests(unittest.TestCase):
    def test_diagnostics_preserve_baseline_and_report_all_variants(self):
        rows = setup_rows()
        counts = {}
        baseline = run(rows)
        self.assertEqual(baseline, run(rows, diagnostics=counts))
        self.assertEqual(4, counts['sessions'])
        self.assertEqual(len(baseline), counts['filled_entries'])
        report = compare_variants(rows, Config())
        self.assertEqual({'baseline', 'qualified_breakout', 'wider_stop', 'selective'}, set(report['variants']))
        for variant in report['variants'].values():
            self.assertEqual(variant['all']['trades'], variant['development']['trades'] + variant['validation']['trades'])
        self.assertEqual(summarize(baseline), report['variants']['baseline']['all'])

    def test_wider_stop_is_separate_and_keeps_two_r_target(self):
        baseline = run(setup_rows())[0]
        wider = run(setup_rows(), Config(stop_buffer_atr=0.25))[0]
        self.assertLess(wider['stop'], baseline['stop'])
        self.assertAlmostEqual(wider['target']-wider['entry'], 2*wider['risk'])

    def test_selective_rejects_flat_opening_and_accepts_directional_opening(self):
        self.assertEqual([], run(setup_rows(), Config(selective=True)))
        rows = setup_rows()
        for i in range(6):
            price = 129+i*.5
            rows[225+i].update(open=price, high=price+.75, low=price-.2, close=price+.6)
        rows[231].update(open=132.3, high=134, low=132, close=133.8)
        trades = run(rows, Config(selective=True))
        self.assertEqual(1, len(trades))
        self.assertAlmostEqual(trades[0]['target']-trades[0]['entry'], 2*trades[0]['risk'])

    def test_minute_aggregation_ist_and_ohlc(self):
        start = datetime.fromisoformat('2026-10-23T03:45:00+00:00')
        rows = [dict(date=(start+timedelta(minutes=i)).isoformat(), open=100+i,
                     high=102+i, low=99+i, close=101+i) for i in range(10)]
        result = aggregate_minutes(rows)
        self.assertEqual(2, len(result))
        self.assertEqual('2026-10-23T09:15:00+05:30', result[0]['date'])
        self.assertEqual((100, 106, 99, 105), tuple(result[0][k] for k in ('open', 'high', 'low', 'close')))
        self.assertEqual('2026-10-23T09:20:00+05:30', result[1]['date'])
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            aggregate_minutes(rows[:4])
        with self.assertRaisesRegex(ValueError, 'chronological'):
            aggregate_minutes([rows[0], rows[0]])

    def test_long_and_short_target(self):
        for short in (False, True):
            with self.subTest(short=short):
                trades = run(setup_rows(short))
                self.assertEqual(len(trades), 1)
                self.assertEqual(trades[0]['direction'], -1 if short else 1)
                self.assertEqual(trades[0]['reason'], 'target')
                self.assertEqual(trades[0]['entry_time'].minute, 55)
                self.assertLess(trades[0]['r'], 2)  # adverse exit slippage

    def test_future_prices_do_not_change_existing_entry(self):
        rows = setup_rows()
        before = run(rows)[0]
        for b in rows[235:]:
            b.update(open=140, high=150, low=139, close=145)
        after = run(rows)[0]
        for field in ('entry_time', 'entry', 'risk', 'stop', 'target'):
            self.assertEqual(before[field], after[field])

    def test_stop_first_and_gap(self):
        p = dict(direction=1, stop=95, target=110)
        self.assertEqual(exit_fill(p, dict(open=100, low=90, high=115), Config()), (94.5, 'stop'))
        self.assertEqual(exit_fill(p, dict(open=90, low=89, high=96), Config()), (89.5, 'stop'))
        self.assertIsNone(exit_fill(p, dict(open=100, low=99, high=115), Config(), entry_bar=True))

    def test_short_gap(self):
        p = dict(direction=-1, stop=105, target=90)
        self.assertEqual(exit_fill(p, dict(open=110, low=89, high=115), Config()), (110.5, 'stop'))

    def test_reject_missing_duplicate_and_bad_ohlc(self):
        for change in ('missing', 'duplicate', 'ohlc'):
            rows = candles()
            if change == 'missing':
                rows.pop(7)
            elif change == 'duplicate':
                rows[7]['date'] = rows[6]['date']
            else:
                rows[7]['low'] = 1000
            with self.assertRaises(ValueError):
                run(rows)

    def test_vwap_requires_volume_and_invalid_config(self):
        with self.assertRaises(KeyError):
            run(candles(), Config(use_vwap=True))
        with self.assertRaises(ValueError):
            Config(slippage=-1)
        self.assertIsNone(summarize([])['win_rate_pct'])


if __name__ == '__main__':
    unittest.main()
