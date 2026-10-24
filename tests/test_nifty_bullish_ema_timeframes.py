from datetime import date,datetime,timedelta
from zoneinfo import ZoneInfo
import unittest

from backtest.nifty_bullish_ema_timeframes import run
from backtest.nifty_daily_ema import run as daily_baseline,Config
from backtest.nifty_30m_put_comparison import run as pe_run

IST=ZoneInfo('Asia/Kolkata')


class BullishTimeframeTests(unittest.TestCase):
    def intraday(self,prices):
        rows=[]
        for day in range(6):
            start=datetime(2026,1,1,9,15,tzinfo=IST)+timedelta(days=day)
            values=[100]*25 if day<5 else prices+[prices[-1]]*(25-len(prices))
            for i,p in enumerate(values):
                rows.append(dict(date=start+timedelta(minutes=15*i),duration_minutes=15,
                                 open=p,high=p+1,low=p-1,close=p))
        return rows

    def test_single_price_cross_is_calls_only_with_next_open_and_adverse_slippage(self):
        rows=self.intraday([110,115,90,85])
        for period in (12,36):
            summary,trades,signals,curve=run(rows,1,period,timeframe='15m',evaluation_start='2026-01-06')
            self.assertEqual(1,len(trades))
            self.assertEqual('CE',trades[0]['side'])
            self.assertEqual(115.5,trades[0]['entry'])
            self.assertEqual(84.5,trades[0]['exit'])
            self.assertEqual(-31,trades[0]['points'])
            self.assertEqual(110,signals[0]['fast'])
            self.assertEqual('cash',curve[-1]['position'])
            self.assertEqual(0,summary['entry_rule_violations'])

    def test_daily20_50_entry_exit_identity_matches_existing_calls_baseline(self):
        prices=[100]*250+[120,130,140,80,70,60,120,130]
        rows=[dict(date=str(date(2020,1,1)+timedelta(days=i)),open=p,high=p+1,low=p-1,close=p) for i,p in enumerate(prices)]
        start=rows[250]['date']
        _,reference,_,_=daily_baseline(rows,Config(fast=20,slow=50,fee_bps=0,slippage_bps=0),evaluation_start=start)
        _,trades,_,_=run(rows,20,50,evaluation_start=start,slippage_points=0)
        self.assertTrue(reference)
        expected=[(t['entry_date'],t['exit_date'],t['entry'],t['exit'],t['points']) for t in reference]
        actual=[(t['entry_time'].date(),t['exit_time'].date(),t['entry'],t['exit'],t['points']) for t in trades]
        self.assertEqual(expected,actual)

    def test_warmup_trends_do_not_cause_retrospective_entry(self):
        rows=self.intraday([110,110,110])
        # Begin after the one upward cross: all trend-only observations stay flat.
        rows=[dict(r,close=110,open=110,high=111,low=109) for r in rows]
        summary,trades,signals,curve=run(rows,9,21,timeframe='15m',evaluation_start='2026-01-06')
        self.assertEqual([],signals)
        self.assertEqual([],trades)
        self.assertIsNone(summary['open_position'])

    def test_final_cross_waits_for_next_open_and_does_not_invent_a_trade(self):
        rows=self.intraday([100])
        rows[-1]=dict(rows[-1],open=110,close=110,high=111,low=109)
        summary,trades,signals,_=run(rows,1,12,timeframe='15m',evaluation_start='2026-01-06')
        self.assertEqual([],trades)
        self.assertEqual('enter_call_signal',summary['pending_signal']['action'])
        self.assertIsNone(summary['open_position'])

    def test_invalid_period_or_duplicate_candle_rejected(self):
        rows=self.intraday([110,115,90,85])
        with self.assertRaises(ValueError):run(rows,50,20,timeframe='15m')
        rows.insert(10,rows[10])
        with self.assertRaises(ValueError):run(rows,9,21,timeframe='15m',evaluation_start='2026-01-06')

    def test_mirrored_bearish_and_bullish_paths_have_symmetric_signal_pnl(self):
        calls=self.intraday([110,115,90,85])
        puts=[dict(r,open=200-r['open'],close=200-r['close'],high=200-r['low'],low=200-r['high']) for r in calls]
        _,ce,_,_=run(calls,1,12,timeframe='15m',evaluation_start='2026-01-06')
        _,pe,_,_=pe_run(puts,1,12,timeframe_minutes=15,evaluation_start='2026-01-06')
        self.assertEqual([t['points'] for t in ce],[t['points'] for t in pe])


if __name__=='__main__':unittest.main()
