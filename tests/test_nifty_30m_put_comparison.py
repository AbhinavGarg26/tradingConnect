import unittest
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from backtest.nifty_30m_put_comparison import aggregate_30m,run

IST=ZoneInfo('Asia/Kolkata')


def bars():
    result=[]
    for day,prices in enumerate(([100]*13,[90,85,110,115,120,110,90,85,115,120,100,90,100])):
        start=datetime(2026,1,1+day,9,15,tzinfo=IST)
        for i,p in enumerate(prices):
            result.append(dict(date=start+timedelta(minutes=30*i),duration_minutes=15 if i==12 else 30,
                               open=p,high=p+1,low=p-1,close=p))
    return result


class ThirtyMinutePutTests(unittest.TestCase):
    def test_session_aggregation_13_candles_and_utc_to_ist(self):
        start=datetime.fromisoformat('2026-01-01T03:45:00+00:00')
        rows=[dict(date=start+timedelta(minutes=i),open=100+i,high=102+i,low=99+i,close=101+i)
              for i in range(375)]
        result=aggregate_30m(rows)
        self.assertEqual(13,len(result))
        self.assertEqual((9,15),(result[0]['date'].hour,result[0]['date'].minute))
        self.assertEqual(30,result[0]['duration_minutes'])
        self.assertEqual(15,result[-1]['duration_minutes'])
        self.assertEqual((100,131,99,130),tuple(result[0][k] for k in ('open','high','low','close')))
        with self.assertRaises(ValueError):
            aggregate_30m(rows[:-1])

    def test_bearish_means_fast_below_slow_and_next_open_fill(self):
        report,trades,signals,curve=run(bars(),2,3,evaluation_start='2026-01-02',slippage_points=0)
        entries=[s for s in signals if s['action']=='enter_put_signal']
        self.assertTrue(entries)
        self.assertTrue(all(s['previous_fast']>=s['previous_slow'] and s['fast']<s['slow'] for s in entries))
        self.assertEqual(datetime(2026,1,2,9,45,tzinfo=IST),trades[0]['entry_time'])
        self.assertEqual(85,trades[0]['entry'])
        self.assertEqual(115,trades[0]['exit'])
        self.assertEqual(-30,trades[0]['points'])
        self.assertTrue(all(t['side']=='PE' for t in trades))

    def test_last_cross_pending_without_invented_fill(self):
        report,trades,*_=run(bars()[:14],2,3,evaluation_start='2026-01-02')
        self.assertEqual([],trades)
        self.assertIsNone(report['open_position'])
        self.assertEqual('enter_put_signal',report['pending_signal']['action'])

    def test_future_data_does_not_change_previous_signals_or_curve(self):
        _,_,signals,curve=run(bars(),2,3,evaluation_start='2026-01-02')
        _,_,prefix_signals,prefix_curve=run(bars()[:18],2,3,evaluation_start='2026-01-02')
        self.assertEqual(prefix_curve,curve[:len(prefix_curve)])
        self.assertEqual(prefix_signals,[s for s in signals if s['time']<=prefix_curve[-1]['time']])

    def test_final_partial_bar_signal_fills_next_session_open(self):
        source=bars()[:13]
        start=datetime(2026,1,2,9,15,tzinfo=IST)
        for i in range(13):
            price=90 if i==12 else 100
            source.append(dict(date=start+timedelta(minutes=30*i),duration_minutes=15 if i==12 else 30,
                               open=price,high=price+1,low=price-1,close=price))
        next_open=datetime(2026,1,3,9,15,tzinfo=IST)
        source.append(dict(date=next_open,duration_minutes=30,open=88,high=89,low=87,close=88))
        report,trades,signals,curve=run(source,2,3,evaluation_start='2026-01-02')
        self.assertEqual(datetime(2026,1,2,15,30,tzinfo=IST),signals[0]['time'])
        self.assertEqual(next_open,report['open_position']['entry_time'])
        self.assertEqual(87.5,report['open_position']['entry'])
        self.assertEqual([],trades)

    def test_bad_order_and_insufficient_warmup_rejected(self):
        source=bars()
        source[14]['date']=source[13]['date']
        with self.assertRaises(ValueError):
            run(source,2,3,evaluation_start='2026-01-02')
        with self.assertRaises(ValueError):
            run(bars(),20,50,evaluation_start='2026-01-02')


if __name__=='__main__':
    unittest.main()
