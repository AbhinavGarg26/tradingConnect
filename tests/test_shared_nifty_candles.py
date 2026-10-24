import unittest
from unittest.mock import Mock,patch
from datetime import timedelta
from sqlalchemy import text,select
from tests.test_database_candles import DatabaseCandlesTests
from market.shared_nifty_candles import FetchState,ensure_minutes,sync_current_session
from market.database_candles import read_completed_15m,CandleNotReady

class SharedMinuteTests(unittest.TestCase):
    def setUp(self):
        DatabaseCandlesTests.setUp(self);FetchState.__table__.create(self.engine)
        self.now=self.open+timedelta(minutes=30,seconds=8)
        self.kite=Mock()
        self.kite.historical_data.return_value=[dict(date=self.open+timedelta(minutes=i),open=100+i,high=102+i,low=99+i,close=101+i) for i in range(30)]
    tearDown=DatabaseCandlesTests.tearDown
    def delete(self):
        with self.factory() as db:db.execute(text('DELETE FROM market_snapshots'))
    def ensure(self,now=None):return ensure_minutes(self.kite,self.open,self.open+timedelta(minutes=30),now or self.now,factory=self.factory)
    def test_existing_data_never_fetches_or_rewrites(self):
        self.assertEqual(0,self.ensure()['fetched']);self.kite.historical_data.assert_not_called()
    def test_multiple_callers_fetch_save_once(self):
        self.delete();first=self.ensure();second=self.ensure()
        self.assertTrue(first['complete']);self.assertEqual(30,first['saved']);self.assertEqual(0,second['fetched'])
        self.kite.historical_data.assert_called_once()
        with self.factory() as db:self.assertEqual(30,db.execute(text('SELECT COUNT(*) FROM market_snapshots')).scalar())
    def test_gap_fetch_only_requested_minutes(self):
        with self.factory() as db:db.execute(text('DELETE FROM market_snapshots WHERE close_price=120'))
        result=self.ensure();self.assertEqual(1,result['saved'])
        args=self.kite.historical_data.call_args.args
        self.assertEqual(self.open+timedelta(minutes=19),args[1]);self.assertEqual(self.open+timedelta(minutes=20),args[2])
    def test_failure_cooldown_survives_another_caller(self):
        self.delete();self.kite.historical_data.side_effect=TimeoutError('secret URL')
        self.assertFalse(self.ensure()['complete']);self.assertTrue(self.ensure()['cooldown'])
        self.kite.historical_data.assert_called_once()
        with self.factory() as db:self.assertEqual('TimeoutError',db.scalar(select(FetchState)).last_error_code)
    def test_incomplete_response_retries_only_remaining_gap_after_cooldown(self):
        self.delete();self.kite.historical_data.return_value=self.kite.historical_data.return_value[:15]
        self.assertFalse(self.ensure()['complete']);self.assertTrue(self.ensure()['cooldown'])
        self.kite.historical_data.return_value=[dict(date=self.open+timedelta(minutes=i),open=100+i,high=102+i,low=99+i,close=101+i) for i in range(15,30)]
        self.assertTrue(self.ensure(self.now+timedelta(seconds=11))['complete'])
        self.assertEqual(self.open+timedelta(minutes=15),self.kite.historical_data.call_args.args[1])
    def test_readthrough_fills_and_second_strategy_reuses(self):
        self.delete()
        bars=read_completed_15m(self.now,factory=self.factory,kite=self.kite)
        self.assertEqual(2,len(bars))
        self.assertEqual(bars,read_completed_15m(self.now,factory=self.factory,kite=self.kite))
        self.kite.historical_data.assert_called_once()
    def test_readiness_never_fetches_or_writes_missing_data(self):
        self.delete()
        with self.assertRaises(CandleNotReady):read_completed_15m(self.now,factory=self.factory,kite=self.kite,persist=False)
        self.kite.historical_data.assert_not_called()
    def test_active_minute_cannot_be_fetched(self):
        with self.assertRaises(ValueError):ensure_minutes(self.kite,self.open,self.open+timedelta(minutes=1),self.open+timedelta(minutes=1,seconds=4),factory=self.factory)
        self.kite.historical_data.assert_not_called()
    def test_sync_current_session_catches_up_from_last_closed_minute(self):
        self.delete();result=sync_current_session(self.kite,self.now,factory=self.factory)
        self.assertTrue(result['complete']);self.assertEqual(30,result['saved'])

    def test_fetcher_job_updates_rows_and_strategy_reuses_them(self):
        import ast
        from pathlib import Path
        tree=ast.parse(Path('kite_market_fetcher.py').read_text())
        function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='refresh_shared_nifty_minutes')
        fixed=self.now
        class Clock(type(self.now)):
            @classmethod
            def now(cls,tz=None):return fixed
        namespace={'datetime':Clock,'IST':self.open.tzinfo,'get_db':self.factory,
                   'fetch_user_token':Mock(return_value=(self.kite,1)),'log':Mock()}
        exec(compile(ast.Module(body=[function],type_ignores=[]),'fetcher-job','exec'),namespace)
        self.delete()
        with patch('trading.ema_feed.Calendar',return_value=lambda day:True):
            namespace['refresh_shared_nifty_minutes']()
            namespace['refresh_shared_nifty_minutes']()
        self.assertTrue(self.ensure()['complete']);self.kite.historical_data.assert_called_once()

    def test_minute_strategies_share_data_and_process_only_new_boundary(self):
        from market.database_candles import MinuteFeed
        self.delete()
        first=MinuteFeed(self.kite,factory=self.factory);second=MinuteFeed(self.kite,factory=self.factory)
        a=first.read(self.now);b=second.read(self.now)
        self.assertEqual(30,len(a));self.assertEqual(a,b);self.kite.historical_data.assert_called_once()
        self.assertEqual([],first.read(self.now+timedelta(seconds=3),after=a[-1]['date']))
        new_stamp=self.open+timedelta(minutes=30)
        self.kite.historical_data.return_value=[dict(date=new_stamp,open=130,high=132,low=129,close=131)]
        new=first.read(self.now+timedelta(minutes=1),after=a[-1]['date'])
        self.assertEqual([new_stamp],[r['date'] for r in new]);self.assertEqual(2,self.kite.historical_data.call_count)
        self.assertEqual(new,second.read(self.now+timedelta(minutes=1),after=b[-1]['date']))
        self.assertEqual(2,self.kite.historical_data.call_count)

    def test_minute_delay_blocks_signals_and_has_bounded_wait(self):
        from market.database_candles import MinuteFeed
        self.delete();self.kite.historical_data.side_effect=TimeoutError()
        feed=MinuteFeed(self.kite,factory=self.factory)
        self.assertEqual([],feed.read(self.now));self.assertTrue(feed.blocked)
        self.assertEqual([],feed.read(self.now+timedelta(seconds=3)))
        with self.assertRaisesRegex(RuntimeError,'90 seconds'):feed.read(self.now+timedelta(seconds=90))

    def test_minute_readiness_has_no_backfill(self):
        from market.database_candles import read_completed_minutes
        self.delete()
        with self.assertRaises(CandleNotReady):read_completed_minutes(self.now,kite=self.kite,factory=self.factory,persist=False)
        self.kite.historical_data.assert_not_called()

    def test_all_nifty_strategy_feeds_delegate_historical_requests(self):
        import ast
        from pathlib import Path
        for path in ('nifty_15m_live.py','nifty_30m_reference_live.py','fixed_profit_filtered_live.py',
                     'ema_strategy_runner.py','trading/ema_feed.py','trading/ema_indicator_cache.py'):
            tree=ast.parse(Path(path).read_text())
            self.assertFalse(any(isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute)
                and node.func.attr=='historical_data' for node in ast.walk(tree)),path)
