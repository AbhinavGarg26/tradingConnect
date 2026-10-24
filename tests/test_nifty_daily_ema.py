import unittest
from datetime import datetime, timedelta
from backtest.nifty_daily_ema import Config, daily_from_minutes, run


def candles(prices):
    return [dict(date=str((datetime(2026, 1, 1)+timedelta(days=i)).date()),
                 open=p, high=p+1, low=p-1, close=p) for i, p in enumerate(prices)]


class DailyEMATests(unittest.TestCase):
    def test_crossovers_fill_next_open_and_hold_overnight(self):
        rows = candles([10, 9, 8, 10, 12, 11, 8, 7, 10, 12])
        report, trades, curve, signals = run(rows, Config(fast=2, slow=3, fee_bps=0))
        self.assertEqual(1, len(trades))
        t = trades[0]
        self.assertEqual('2026-01-04', str(t['signal_date']))
        self.assertEqual('2026-01-05', str(t['entry_date']))
        self.assertEqual(12, t['entry'])
        self.assertEqual('2026-01-08', str(t['exit_date']))
        self.assertEqual(7, t['exit'])
        self.assertEqual(3, t['holding_sessions'])
        self.assertIsNotNone(report['open_position'])
        self.assertEqual(1, report['closed_trades'])  # final position not counted as a win/loss
        self.assertGreater(report['max_daily_close_drawdown_pct'], 0)

    def test_signal_has_no_same_close_fill_and_gap_is_paid(self):
        rows = candles([10, 9, 8, 10, 12, 11, 8, 7])
        rows[4].update(open=20, high=21)
        report, trades, *_ = run(rows, Config(fast=2, slow=3, fee_bps=0, slippage_bps=10))
        self.assertAlmostEqual(20*1.001, trades[0]['entry'])
        self.assertAlmostEqual(7*.999, trades[0]['exit'])

    def test_end_signal_pending_and_no_initial_trend_entry(self):
        report, trades, *_ = run(candles([10, 9, 8, 10]), Config(fast=2, slow=3))
        self.assertEqual('buy', report['pending_signal']['action'])
        self.assertIsNone(report['open_position'])
        self.assertEqual([], trades)
        report, trades, *_ = run(candles([10, 11, 12, 13, 14]), Config(fast=2, slow=3))
        self.assertIsNone(report['open_position'])
        self.assertEqual([], trades)

    def test_future_prices_do_not_change_prior_equity_or_signals(self):
        rows = candles([10, 9, 8, 10, 12, 11, 8, 7, 10, 12])
        _, _, curve, signals = run(rows, Config(fast=2, slow=3))
        _, _, prefix, first_signals = run(rows[:7], Config(fast=2, slow=3))
        self.assertEqual(prefix, curve[:7])
        self.assertEqual(first_signals, [s for s in signals if s['date'] <= prefix[-1]['date']])

    def test_costs_reduce_equity(self):
        rows = candles([10, 9, 8, 10, 12, 11, 8, 7])
        free, *_ = run(rows, Config(fast=2, slow=3, fee_bps=0))
        paid, *_ = run(rows, Config(fast=2, slow=3, fee_bps=10))
        self.assertLess(paid['ending_marked_equity'], free['ending_marked_equity'])

    def test_minute_aggregation_and_missing_rejected(self):
        start = datetime.fromisoformat('2026-01-01T09:15:00+05:30')
        rows = [dict(date=(start+timedelta(minutes=i)).isoformat(),open=100+i,
                     high=102+i,low=99+i,close=101+i) for i in range(375)]
        daily = daily_from_minutes(rows)
        self.assertEqual(1, len(daily))
        self.assertEqual((100, 476, 99, 475), tuple(daily[0][k] for k in ('open','high','low','close')))
        with self.assertRaises(ValueError):
            daily_from_minutes(rows[:-1])

    def test_invalid_periods_and_daily_candles(self):
        with self.assertRaises(ValueError):
            Config(fast=50, slow=20)
        rows = candles([10]*60)
        rows[2]['date'] = rows[1]['date']
        with self.assertRaises(ValueError):
            run(rows)

    def test_common_evaluation_start_uses_warmup_without_early_trading(self):
        rows = candles([10, 9, 8, 10, 12, 11, 8, 7, 10, 12])
        reports = []
        for slow in (3, 5):
            report, trades, curve, signals = run(rows, Config(fast=2, slow=slow), evaluation_start='2026-01-07')
            self.assertEqual('2026-01-07', str(curve[0]['date']))
            self.assertEqual(100000, curve[0]['equity'])
            self.assertTrue(all(s['date'] >= curve[0]['date'] for s in signals))
            self.assertTrue(all(t['entry_date'] > curve[0]['date'] for t in trades))
            reports.append(report)
        self.assertEqual(reports[0]['buy_hold_start'], reports[1]['buy_hold_start'])
        self.assertEqual(reports[0]['buy_hold_marked_return_pct'], reports[1]['buy_hold_marked_return_pct'])
        with self.assertRaises(ValueError):
            run(rows, Config(fast=2, slow=5), evaluation_start='2026-01-03')


if __name__ == '__main__':
    unittest.main()
