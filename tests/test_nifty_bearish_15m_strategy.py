import csv
import unittest
from datetime import datetime,timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from strategies.nifty_bearish_15m import Bearish15m,ExecutionProfile,decision,readiness

IST=ZoneInfo('Asia/Kolkata')
ROOT=Path(__file__).resolve().parents[1]


class BearishStrategyTests(unittest.TestCase):
    def test_packaged_signals_match_frozen_backtest_exactly(self):
        engine=Bearish15m()
        actual=[]
        observed=datetime(2026,10,24,12,tzinfo=IST)
        with (ROOT/'outputs/nifty_15m_put_comparison/candles_15m.csv').open() as f:
            for row in csv.DictReader(f):
                state=engine.on_bar(row,observed_at=observed)
                if state['candle_open'].date()<datetime(2021,1,1).date():
                    continue
                if state['cross_down']:
                    actual.append(('enter_put_signal',state['signal_time'].isoformat()))
                elif state['cross_up']:
                    actual.append(('exit_to_cash',state['signal_time'].isoformat()))
        with (ROOT/'outputs/nifty_15m_put_comparison/9_21/signals.csv').open() as f:
            expected=[(r['action'],datetime.fromisoformat(r['time']).isoformat()) for r in csv.DictReader(f)]
        # First bullish cross while starting flat does not exit a position.
        while actual and actual[0][0]=='exit_to_cash': actual.pop(0)
        self.assertEqual(expected,actual)

    def test_incomplete_duplicate_and_gapped_candles_rejected(self):
        start=datetime(2026,10,23,9,15,tzinfo=IST)
        bar=dict(date=start,open=100,high=101,low=99,close=100)
        engine=Bearish15m()
        with self.assertRaises(ValueError):
            engine.on_bar(bar,observed_at=start+timedelta(minutes=14))
        engine.on_bar(bar,observed_at=start+timedelta(minutes=15))
        with self.assertRaises(ValueError):
            engine.on_bar(bar,observed_at=start+timedelta(minutes=30))
        with self.assertRaises(ValueError):
            engine.on_bar(dict(bar,date=start+timedelta(minutes=30)),observed_at=start+timedelta(minutes=45))

    def test_stale_historical_signal_never_becomes_current_entry(self):
        source=ROOT/'outputs/nifty_15m_put_comparison/candles_15m.csv'
        with source.open() as f: rows=list(csv.DictReader(f))
        result=readiness(rows,datetime(2026,10,24,12,tzinfo=IST))
        self.assertFalse(result['signal_fresh'])
        self.assertFalse(result['real_orders_enabled'])
        self.assertEqual('wait_for_fresh_completed_candle',result['current_decision'])

    def test_put_only_state_machine_and_disabled_profile(self):
        state=dict(warmed_up=True,cross_down=True,cross_up=False)
        self.assertEqual('enter_put',decision(state))
        self.assertEqual('hold_put',decision(state,owned_put=True))
        self.assertEqual('exit_put',decision(dict(state,cross_down=False,cross_up=True),owned_put=True))
        self.assertEqual('wait',decision(dict(state,cross_down=False,cross_up=True)))
        with self.assertRaises(ValueError): ExecutionProfile(activation='live')
        with self.assertRaises(ValueError): ExecutionProfile(contract_side='CE')


if __name__=='__main__':
    unittest.main()
