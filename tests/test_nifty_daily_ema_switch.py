import unittest
from backtest.nifty_daily_ema_switch import run
from tests.test_nifty_daily_ema import candles


class DailySwitchTests(unittest.TestCase):
    def test_puts_only_enters_bearish_and_exits_to_cash(self):
        rows=candles([10,9,8,10,12,11,8,7,10,12])
        report,trades,signals,curve=run(rows,evaluation_start='2026-01-04',fast=2,slow=3,slippage_points=0,puts_only=True)
        self.assertEqual(['PE'],[t['side'] for t in trades])
        self.assertEqual('2026-01-08',str(trades[0]['entry_date']))
        self.assertEqual('2026-01-10',str(trades[0]['exit_date']))
        self.assertIsNone(report['open_signal_position'])
        self.assertEqual('flat',curve[-1]['side'])
        self.assertEqual(0,report['call_side']['closed_trades'])
        self.assertTrue(all(b['side']=='flat' for b in curve[:4]))

    def test_puts_only_matches_bearish_subset_of_two_sided_signals(self):
        rows=candles([10,9,8,10,12,11,8,7,10,12])
        _,both,*_=run(rows,evaluation_start='2026-01-04',fast=2,slow=3)
        _,puts,*_=run(rows,evaluation_start='2026-01-04',fast=2,slow=3,puts_only=True)
        self.assertEqual([t for t in both if t['side']=='PE'],puts)

    def test_flips_both_directions_on_next_open(self):
        rows=candles([10,9,8,10,12,11,8,7,10,12])
        report,trades,signals,curve=run(rows,evaluation_start='2026-01-04',fast=2,slow=3,slippage_points=0)
        self.assertEqual(['CE','PE'],[t['side'] for t in trades])
        self.assertEqual('2026-01-05',str(trades[0]['entry_date']))
        self.assertEqual('2026-01-08',str(trades[0]['exit_date']))
        self.assertEqual(trades[0]['exit_date'],trades[1]['entry_date'])
        self.assertEqual(-5,trades[0]['points'])
        self.assertEqual(-5,trades[1]['points'])
        self.assertEqual('CE',report['open_signal_position']['side'])
        self.assertEqual(2,report['combined']['closed_trades'])

    def test_final_cross_stays_pending(self):
        report,trades,*_=run(candles([10,9,8,10]),evaluation_start='2026-01-04',fast=2,slow=3)
        self.assertEqual([],trades)
        self.assertIsNone(report['open_signal_position'])
        self.assertEqual(1,report['pending_signal']['direction'])

    def test_no_catchup_trade_when_starting_in_existing_trend(self):
        report,trades,*_=run(candles([10,11,12,13,14]),evaluation_start='2026-01-04',fast=2,slow=3)
        self.assertEqual([],trades)
        self.assertIsNone(report['open_signal_position'])

    def test_bearish_signal_can_be_first_entry(self):
        report,trades,*_=run(candles([10,11,12,9,8]),evaluation_start='2026-01-04',fast=2,slow=3)
        self.assertEqual('PE',report['open_signal_position']['side'])
        self.assertEqual([],trades)

    def test_future_data_does_not_change_earlier_curve(self):
        rows=candles([10,9,8,10,12,11,8,7,10,12])
        _,_,_,full=run(rows,evaluation_start='2026-01-04',fast=2,slow=3)
        _,_,_,prefix=run(rows[:7],evaluation_start='2026-01-04',fast=2,slow=3)
        self.assertEqual(prefix,full[:len(prefix)])

    def test_gap_prices_and_side_summaries(self):
        rows=candles([10,9,8,10,12,11,8,7,10,12])
        rows[4].update(open=9,low=8)
        rows[7].update(open=14,high=15)
        rows[9].update(open=8,low=7)
        report,trades,*_=run(rows,evaluation_start='2026-01-04',fast=2,slow=3,slippage_points=0)
        self.assertEqual([5,6],[t['points'] for t in trades])
        self.assertEqual(100,report['combined']['win_rate_pct'])
        self.assertEqual(report['combined']['net_index_points'],report['call_side']['net_index_points']+report['put_side']['net_index_points'])
        self.assertEqual('blocked_missing_historical_option_prices',report['actual_monthly_option_backtest_status'])
