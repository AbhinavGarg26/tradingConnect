import os
os.environ.setdefault('DB_ENCRYPTION_KEY','unit-test-key-not-used-for-db')
import unittest
from unittest.mock import patch,Mock
from datetime import date,datetime,timedelta
from zoneinfo import ZoneInfo
from trading.ema_feed import load_warmup_history,Feed
from backtest.nifty_daily_ema import daily_from_minutes
from backtest.nifty_30m_put_comparison import aggregate_session
from strategies.nifty_bearish_15m import Bearish15m

IST=ZoneInfo('Asia/Kolkata')

def minutes(start,end):
    rows=[];day=start
    while day<=end:
        if day.weekday()<5:
            opening=datetime.combine(day,datetime.min.time(),IST).replace(hour=9,minute=15)
            for i in range(375):
                close=100+(day.toordinal()%17)+((i//30)%3)
                rows.append(dict(date=opening+timedelta(minutes=i),open=close,high=close+2,low=close-2,close=close))
        day+=timedelta(days=1)
    return rows

class WarmupTests(unittest.TestCase):
    def test_bounded_windows_match_full_history_daily_and_15m_exactly(self):
        start=date(2026,7,1);end=date(2026,9,30)
        source=minutes(start,end)
        for kind in ('daily_calls','bearish_puts'):
            calls=[]
            def load(first,last,**kwargs):
                self.assertLessEqual((last-first).days,29)
                self.assertTrue(kwargs['database_only'])
                calls.append((first,last))
                return [row for row in source if first<=row['date'].date()<=last]
            with patch('trading.ema_feed.load_snapshot_candles',side_effect=load):
                result=load_warmup_history(Mock(),kind,end,start=start)
            expected=daily_from_minutes(source) if kind=='daily_calls' else aggregate_session(source,15)
            self.assertEqual(expected,result)
            self.assertEqual(4,len(calls))
    def test_snapshot_results_match_frozen_engine_and_only_keep_needed_rows(self):
        source=aggregate_session(minutes(date(2026,9,1),date(2026,9,30)),15)
        feed=Feed.__new__(Feed);feed.kind='bearish_puts';feed.history=source
        feed.day=date(2026,10,1);feed.calendar=Mock(return_value=False)
        feed.calendar.next_open.side_effect=lambda day:datetime.combine(day+timedelta(days=1),datetime.min.time(),IST).replace(hour=9,minute=15)
        now=datetime(2026,10,1,8,0,tzinfo=IST)
        reference=Bearish15m();states=[]
        for row in source:
            state=reference.on_bar(row,observed_at=now)
            if state['warmed_up']:states.append(state)
        latest=feed.snapshots(now)
        self.assertEqual(1,len(latest))
        self.assertEqual(states[-1]['fast_ema'],latest[0]['fast'])
        self.assertEqual(states[-1]['cross_down'],latest[0]['enter'])
        after=states[-4]['signal_time']
        catchup=feed.snapshots(now,after)
        self.assertEqual([r['signal_time'] for r in states[-3:]],[r['time'] for r in catchup])

    def test_importing_ema_strategy_does_not_load_unrelated_dataframe_stack(self):
        import subprocess,sys,json
        result=subprocess.run([sys.executable,'-c',
            "import sys,json; import strategies.nifty_bearish_15m; print(json.dumps({'pandas':'pandas' in sys.modules,'numpy':'numpy' in sys.modules}))"],capture_output=True,text=True,check=True)
        self.assertEqual(dict(pandas=False,numpy=False),json.loads(result.stdout))

    def test_existing_adx_package_export_still_works(self):
        from strategies import ADXCrossoverStrategy
        from strategies.adx_crossover import ADXCrossoverStrategy as Direct
        self.assertIs(Direct,ADXCrossoverStrategy)
