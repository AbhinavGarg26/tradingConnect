import unittest
from datetime import date,datetime,timedelta
from zoneinfo import ZoneInfo
from backtest.nifty_30m_put_comparison import run as intraday_run
from backtest.nifty_daily_ema_switch import run as daily_run

IST=ZoneInfo('Asia/Kolkata')


class PriceSingleEMATests(unittest.TestCase):
    def test_price_crosses_each_single_ema_not_ema12_crossing_ema36(self):
        bars=[]
        for day in range(6):
            opening=datetime(2026,1,1,tzinfo=IST).replace(hour=9,minute=15)+timedelta(days=day)
            prices=[100]*25 if day<5 else [90,85,110,115]+[120]*21
            for i,p in enumerate(prices):
                bars.append(dict(date=opening+timedelta(minutes=15*i),duration_minutes=15,
                                 open=p,high=p+1,low=p-1,close=p))
        for period in (12,36):
            with self.subTest(period=period):
                report,trades,signals,curve=intraday_run(bars,1,period,evaluation_start='2026-01-06',timeframe_minutes=15,slippage_points=0)
                entries=[s for s in signals if s['action']=='enter_put_signal']
                self.assertEqual(1,len(entries))
                self.assertEqual(90,entries[0]['fast'])  # EMA1 is exactly price, not EMA12.
                self.assertGreater(entries[0]['slow'],entries[0]['fast'])
                self.assertEqual(entries[0]['previous_fast'],entries[0]['previous_slow'])
                self.assertEqual(85,trades[0]['entry'])
                self.assertEqual(115,trades[0]['exit'])
                self.assertEqual(-30,trades[0]['points'])
                self.assertEqual('cash',curve[-1]['position'])

    def test_daily_single_ema_uses_same_price_cross_and_next_open(self):
        prices=[100]*60+[90,85,110,115]
        rows=[dict(date=str(date(2026,1,1)+timedelta(days=i)),open=p,high=p+1,low=p-1,close=p)
              for i,p in enumerate(prices)]
        for period in (12,36):
            with self.subTest(period=period):
                report,trades,signals,curve=daily_run(rows,fast=1,slow=period,evaluation_start=rows[60]['date'],puts_only=True,slippage_points=0)
                self.assertEqual(1,len(trades))
                self.assertEqual('PE',trades[0]['side'])
                self.assertEqual(85,trades[0]['entry'])
                self.assertEqual(115,trades[0]['exit'])
                self.assertEqual(90,signals[0]['fast_ema'])
                self.assertIsNone(report['open_signal_position'])


if __name__=='__main__':
    unittest.main()
