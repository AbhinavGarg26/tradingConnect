import unittest
from datetime import datetime,timedelta
from backtest.nifty_1h_put_comparison import aggregate_hourly
from backtest.nifty_30m_put_comparison import aggregate_30m,run
from tests.test_nifty_30m_put_comparison import bars


class HourlyPutTests(unittest.TestCase):
    def test_hourly_ohlc_matches_minutes_and_preserves_session_tail(self):
        start=datetime.fromisoformat('2026-01-01T03:45:00+00:00')
        minutes=[dict(date=start+timedelta(minutes=i),open=100+i,high=102+i,low=99+i,close=101+i)
                 for i in range(375)]
        hourly=aggregate_hourly(aggregate_30m(minutes))
        self.assertEqual(7,len(hourly))
        self.assertEqual([60]*6+[15],[b['duration_minutes'] for b in hourly])
        self.assertEqual((100,161,99,160),tuple(hourly[0][k] for k in ('open','high','low','close')))
        self.assertEqual((15,15),(hourly[-1]['date'].hour,hourly[-1]['date'].minute))

    def test_no_cross_session_merge_and_bearish_next_bar_entry(self):
        hourly=aggregate_hourly(bars())
        self.assertEqual(14,len(hourly))
        self.assertNotEqual(hourly[6]['date'].date(),hourly[7]['date'].date())
        report,trades,signals,curve=run(hourly,2,3,evaluation_start='2026-01-02',timeframe_minutes=60)
        entries=[s for s in signals if s['action']=='enter_put_signal']
        self.assertTrue(entries)
        self.assertTrue(all(s['previous_fast']>=s['previous_slow'] and s['fast']<s['slow'] for s in entries))
        self.assertEqual('60m_session_aligned',report['timeframe'])
        self.assertEqual(10,trades[0]['entry_time'].hour)
        self.assertEqual(15,trades[0]['entry_time'].minute)

    def test_missing_duplicate_and_wrong_duration_rejected(self):
        for kind in ('missing','duplicate','duration'):
            source=bars()
            if kind=='missing': source.pop(3)
            elif kind=='duplicate': source[2]['date']=source[1]['date']
            else: source[-1]['duration_minutes']=30
            with self.assertRaises(ValueError):
                aggregate_hourly(source)

    def test_hourly_future_data_does_not_change_prior_curve(self):
        hourly=aggregate_hourly(bars())
        _,_,_,full=run(hourly,2,3,evaluation_start='2026-01-02',timeframe_minutes=60)
        _,_,_,prefix=run(hourly[:10],2,3,evaluation_start='2026-01-02',timeframe_minutes=60)
        self.assertEqual(prefix,full[:len(prefix)])


if __name__=='__main__':
    unittest.main()
