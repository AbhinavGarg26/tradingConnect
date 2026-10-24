import unittest
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from backtest.nifty_30m_put_comparison import aggregate_session,run

IST=ZoneInfo('Asia/Kolkata')


def minutes():
    start=datetime.fromisoformat('2026-01-01T03:45:00+00:00')
    return [dict(date=start+timedelta(minutes=i),open=100+i,high=102+i,low=99+i,close=101+i)
            for i in range(375)]


def candles():
    result=[]
    for day in range(2):
        start=datetime(2026,1,1+day,9,15,tzinfo=IST)
        prices=[100]*25 if day==0 else [90,85,110,115,120]+[120]*20
        for i,p in enumerate(prices):
            result.append(dict(date=start+timedelta(minutes=15*i),duration_minutes=15,
                               open=p,high=p+1,low=p-1,close=p))
    return result


class FifteenMinutePutTests(unittest.TestCase):
    def test_25_full_bars_and_ohlc(self):
        bars=aggregate_session(minutes(),15)
        self.assertEqual(25,len(bars))
        self.assertTrue(all(b['duration_minutes']==15 for b in bars))
        self.assertEqual((100,116,99,115),tuple(bars[0][k] for k in ('open','high','low','close')))
        self.assertEqual((9,15),(bars[0]['date'].hour,bars[0]['date'].minute))
        self.assertEqual((15,15),(bars[-1]['date'].hour,bars[-1]['date'].minute))
        with self.assertRaises(ValueError):
            aggregate_session(minutes()[:-1],15)

    def test_fast_cross_below_slow_and_fill_at_next_15m_open(self):
        report,trades,signals,curve=run(candles(),2,3,evaluation_start='2026-01-02',timeframe_minutes=15,slippage_points=0)
        self.assertEqual(1,len(trades))
        self.assertEqual(datetime(2026,1,2,9,30,tzinfo=IST),trades[0]['entry_time'])
        self.assertEqual(datetime(2026,1,2,10,0,tzinfo=IST),trades[0]['exit_time'])
        self.assertEqual(-30,trades[0]['points'])
        entries=[s for s in signals if s['action']=='enter_put_signal']
        self.assertTrue(all(s['previous_fast']>=s['previous_slow'] and s['fast']<s['slow'] for s in entries))
        self.assertEqual('15m_session_aligned',report['timeframe'])
        self.assertEqual('cash',curve[-1]['position'])

    def test_future_candles_cannot_change_prior_curve(self):
        _,_,_,full=run(candles(),2,3,evaluation_start='2026-01-02',timeframe_minutes=15)
        _,_,_,prefix=run(candles()[:28],2,3,evaluation_start='2026-01-02',timeframe_minutes=15)
        self.assertEqual(prefix,full[:len(prefix)])


if __name__=='__main__':
    unittest.main()
