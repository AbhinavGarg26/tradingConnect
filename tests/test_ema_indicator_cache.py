import os
os.environ.setdefault('DB_ENCRYPTION_KEY','unit-test-key-not-used-for-db')
import unittest,tempfile
from contextlib import contextmanager
from datetime import date,datetime,timedelta,timezone
from unittest.mock import patch,Mock
from pathlib import Path
from sqlalchemy import create_engine,select,func,event
from sqlalchemy.orm import sessionmaker
from trading.ema_indicator_cache import IndicatorCache,IndicatorCacheRow,IndicatorCandleRow,CacheStale,CachedFeed
from tests.test_ema_warmup import minutes
from backtest.nifty_daily_ema import daily_from_minutes
from backtest.nifty_30m_put_comparison import aggregate_session
from strategies.nifty_bearish_15m import Bearish15m
from zoneinfo import ZoneInfo
IST=ZoneInfo('Asia/Kolkata')

class Calendar:
    holidays=set()
    def __call__(self,day):return day.weekday()<5
    def next_open(self,day):
        day+=timedelta(days=1)
        while not self(day):day+=timedelta(days=1)
        return datetime.combine(day,datetime.min.time(),IST).replace(hour=9,minute=15)

class IndicatorCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.engine=create_engine('sqlite:///'+str(Path(self.tmp.name,'cache.sqlite')))
        IndicatorCacheRow.__table__.create(self.engine);IndicatorCandleRow.__table__.create(self.engine)
        sessions=sessionmaker(self.engine)
        @contextmanager
        def factory():
            with sessions.begin() as db:yield db
        self.factory=factory;self.calendar=Calendar()
        self.now=datetime(2020,4,1,8,0,tzinfo=IST)
        self.source=minutes(date(2020,1,1),date(2020,4,1));self.calls=[]
        def load(start,end,**kwargs):
            self.calls.append((start,end));self.assertTrue(kwargs['database_only'])
            return [row for row in self.source if start<=row['date'].date()<=end]
        self.loader=patch('trading.ema_indicator_cache.load_snapshot_candles',side_effect=load);self.loader.start()
        self.watermark=datetime(2020,4,1,tzinfo=timezone.utc)
        self.marker=patch.object(IndicatorCache,'_watermark',side_effect=lambda db,through:self.watermark);self.marker.start()
    def tearDown(self):
        self.loader.stop();self.marker.stop();self.engine.dispose();self.tmp.cleanup()
    def cache(self,kind='daily_calls',read_only=False):
        return IndicatorCache(self.factory,kind,self.calendar,skip_incomplete_days=True,read_only=read_only)
    def count(self):
        with self.factory() as db:return db.scalar(select(func.count()).select_from(IndicatorCandleRow))
    def test_bullish_calls_have_separate_cache_and_opposite_put_signals(self):
        calls=self.cache('bullish_calls');puts=self.cache('bearish_puts');daily=self.cache()
        self.assertEqual(3,len({calls.key,puts.key,daily.key}))
        calls.sync_history(self.now);puts.sync_history(self.now)
        self.assertEqual(calls.data['state'],puts.data['state'])
        for up,down,fast,slow in [(True,False,102,100),(False,True,98,100)]:
            for cache in (calls,puts):
                cache.data['state'].update(cross_up=up,cross_down=down,fast=fast,slow=slow)
            call=calls.snapshots(self.now)[0];put=puts.snapshots(self.now)[0]
            self.assertEqual(up,call['enter']);self.assertEqual(down,call['exit'])
            self.assertEqual(up,call['trend_valid'])
            self.assertEqual(put['exit'],call['enter'])
            self.assertEqual(put['enter'],call['exit'])
            self.assertEqual('bullish_calls',call['kind'])

    def test_restart_reuses_all_calculations_without_loading_old_minutes(self):
        cache=self.cache();cache.sync_history(self.now);first=cache.data['state'].copy();count=self.count()
        self.assertEqual(65,count);self.calls.clear()
        restarted=self.cache();restarted.sync_history(self.now)
        self.assertEqual([],self.calls);self.assertEqual(first,restarted.data['state']);self.assertEqual(count,self.count())
    def test_invalid_append_rolls_back_all_candles_and_head(self):
        cache=self.cache('bearish_puts');cache.sync_history(self.now)
        original=cache.data['state'].copy();count=self.count()
        bars=aggregate_session([r for r in self.source if r['date'].date()==self.now.date()],15)
        with self.assertRaisesRegex(ValueError,'Missing intraday'):
            cache.append([bars[0],bars[2]],self.now.replace(hour=11))
        restarted=self.cache('bearish_puts');restarted.sync_history(self.now)
        self.assertEqual(original,restarted.data['state']);self.assertEqual(count,self.count())
        with self.assertRaisesRegex(ValueError,'opening candle'):
            restarted.append([bars[1]],self.now.replace(hour=11))
    def test_next_session_loads_only_new_day_and_preserves_exact_daily_ema(self):
        cache=self.cache();cache.sync_history(self.now);self.calls.clear()
        cache=self.cache();cache.sync_history(self.now+timedelta(days=1))
        self.assertEqual([(date(2020,4,1),date(2020,4,1))],self.calls)
        fast=slow=None
        for row in daily_from_minutes(self.source):
            c=row['close'];fast=c if fast is None else fast+2/21*(c-fast);slow=c if slow is None else slow+2/51*(c-slow)
        self.assertEqual(fast,cache.data['state']['fast']);self.assertEqual(slow,cache.data['state']['slow'])
    def test_15m_cache_matches_every_frozen_engine_state_and_crossover(self):
        cache=self.cache('bearish_puts');cache.sync_history(self.now)
        rows=[row for row in self.source if row['date'].date()<self.now.date()]
        engine=Bearish15m();expected=[]
        for row in aggregate_session(rows,15):expected.append(engine.on_bar(row,observed_at=self.now))
        with self.factory() as db:
            states=[row.state for row in db.scalars(select(IndicatorCandleRow).where(IndicatorCandleRow.cache_key==cache.key).order_by(IndicatorCandleRow.candle_time))]
        for state,reference in zip(states,expected):
            self.assertEqual(reference['fast_ema'],state['fast']);self.assertEqual(reference['slow_ema'],state['slow'])
            self.assertEqual(reference['cross_up'],state['cross_up']);self.assertEqual(reference['cross_down'],state['cross_down'])
        self.assertEqual(len(expected),len(states))
    def test_read_only_cold_and_warm_checks_issue_no_database_writes(self):
        statements=[]
        def observe(conn,cursor,statement,parameters,context,executemany):statements.append(statement)
        event.listen(self.engine,'before_cursor_execute',observe)
        cache=self.cache(read_only=True);cache.sync_history(self.now)
        self.assertEqual(0,self.count())
        self.assertFalse(any(s.lstrip().upper().startswith(('INSERT','UPDATE','DELETE')) for s in statements))
        event.remove(self.engine,'before_cursor_execute',observe)
        self.cache().sync_history(self.now);self.calls.clear();statements.clear()
        event.listen(self.engine,'before_cursor_execute',observe)
        self.cache(read_only=True).sync_history(self.now)
        self.assertEqual([],self.calls)
        self.assertFalse(any(s.lstrip().upper().startswith(('INSERT','UPDATE','DELETE')) for s in statements))
    def test_source_correction_requires_explicit_rebuild(self):
        cache=self.cache();cache.sync_history(self.now)
        self.watermark+=timedelta(hours=1)
        with self.assertRaises(CacheStale):self.cache().sync_history(self.now)
        rebuilt=self.cache();rebuilt.sync_history(self.now,rebuild=True)
        self.assertEqual(65,self.count())
    def test_intraday_append_is_idempotent_and_next_day_does_not_double_count(self):
        cache=self.cache('bearish_puts');cache.sync_history(self.now)
        current=aggregate_session([row for row in self.source if row['date'].date()==self.now.date()],15)[:2]
        now=self.now.replace(hour=10)
        cache.append(current,now);count=cache.data['state']['count']
        cache.append(current,now);self.assertEqual(count,cache.data['state']['count'])
        resumed=self.cache('bearish_puts');resumed.sync_history(self.now+timedelta(days=1))
        self.assertEqual(66*25,resumed.data['state']['count'])
        self.assertEqual(66*25,self.count())
    def test_changed_cached_intraday_price_cannot_be_silently_ignored(self):
        cache=self.cache('bearish_puts');cache.sync_history(self.now)
        row=aggregate_session([r for r in self.source if r['date'].date()==self.now.date()],15)[0]
        cache.append([row],self.now.replace(hour=10));row=dict(row,close=row['close']+0.5)
        with self.assertRaises(CacheStale):cache.append([row],self.now.replace(hour=10))
    def test_read_only_intraday_calculations_keep_seed_and_never_write(self):
        cache=self.cache('bearish_puts',read_only=True);cache.sync_history(self.now)
        current=aggregate_session([r for r in self.source if r['date'].date()==self.now.date()],15)
        count=cache.data['state']['count']
        cache.append(current[:1],self.now.replace(hour=10))
        cache.append(current[:2],self.now.replace(hour=10))
        self.assertEqual(count+2,cache.data['state']['count']);self.assertEqual(0,self.count())
    def test_missing_intraday_bucket_is_rejected(self):
        cache=self.cache('bearish_puts');cache.sync_history(self.now)
        current=aggregate_session([r for r in self.source if r['date'].date()==self.now.date()],15)
        cache.append(current[:1],self.now.replace(hour=11))
        with self.assertRaisesRegex(ValueError,'Missing intraday'):cache.append(current[2:3],self.now.replace(hour=11))
    def test_daily_feed_never_fetches_intraday_quotes_or_recalculates_saved_history(self):
        kite=Mock();feed=CachedFeed(kite,self.factory,'daily_calls',self.calendar,self.now,skip_incomplete_days=True)
        count=self.count();self.calls.clear()
        for i in range(10):feed.snapshots(self.now+timedelta(seconds=20*i))
        self.assertEqual(count,self.count());self.assertEqual([],self.calls);self.assertEqual([],kite.mock_calls)
    def test_intraday_feed_uses_only_new_database_bars_and_never_broker_history(self):
        now=self.now.replace(hour=9,minute=30,second=5)
        kite=Mock();feed=CachedFeed(kite,self.factory,'bearish_puts',self.calendar,now,skip_incomplete_days=True)
        bars=aggregate_session([r for r in self.source if r['date'].date()==now.date()],15)[:1]
        self.calls.clear()
        with patch('market.database_candles.read_completed_15m',return_value=bars) as reader:
            snapshots=feed.snapshots(now);feed.snapshots(now+timedelta(seconds=5))
        reader.assert_called_once();self.assertEqual(bars[0]['date']+timedelta(minutes=15),snapshots[-1]['time'])
        self.assertEqual([],self.calls);self.assertEqual([],kite.mock_calls)
    def test_delayed_database_candle_never_returns_stale_signal_for_execution(self):
        from market.database_candles import CandleNotReady
        now=self.now.replace(hour=9,minute=30,second=5)
        kite=Mock();feed=CachedFeed(kite,self.factory,'bearish_puts',self.calendar,now,skip_incomplete_days=True)
        with patch('market.database_candles.read_completed_15m',side_effect=CandleNotReady('missing')) as reader:
            self.assertEqual([],feed.snapshots(now))
            self.assertEqual([],feed.snapshots(now+timedelta(seconds=5)))
        reader.assert_called_once();self.assertEqual([],kite.mock_calls)
    def test_missing_previous_session_is_filled_once_before_daily_update(self):
        self.cache().sync_history(self.now)
        previous=[r for r in self.source if r['date'].date()==self.now.date()]
        self.source=[r for r in self.source if r['date'].date()!=self.now.date()]
        def refill(*args,**kwargs):self.source.extend(previous);return {'complete':True}
        tomorrow=self.now+timedelta(days=1)
        with patch('market.shared_nifty_candles.ensure_minutes',side_effect=refill) as fill:
            feed=CachedFeed(Mock(),self.factory,'daily_calls',self.calendar,tomorrow,skip_incomplete_days=True)
            CachedFeed(Mock(),self.factory,'daily_calls',self.calendar,tomorrow,skip_incomplete_days=True)
        fill.assert_called_once();self.assertEqual(66,feed.cache.data['state']['count'])
    def test_readiness_never_backfills_missing_previous_session(self):
        self.cache().sync_history(self.now)
        self.source=[r for r in self.source if r['date'].date()!=self.now.date()]
        with patch('market.shared_nifty_candles.ensure_minutes') as fill:
            with self.assertRaisesRegex(ValueError,'Latest completed session'):
                CachedFeed(Mock(),self.factory,'daily_calls',self.calendar,self.now+timedelta(days=1),skip_incomplete_days=True,read_only=True)
        fill.assert_not_called()
    def test_daily_execution_finishes_after_one_confirmed_cycle(self):
        from ema_strategy_runner import finish_daily_run
        store=Mock();store.state.return_value={'pending_signal':{}};store.recoverable.return_value=[]
        self.assertTrue(finish_daily_run('daily_calls',store,self.now))
        store.heartbeat.assert_called_once_with(self.now,'completed')
        self.assertFalse(finish_daily_run('bearish_puts',store,self.now))
        store.state.return_value={'pending_signal':{'action':'entry'}}
        with self.assertRaisesRegex(RuntimeError,'reconciliation'):finish_daily_run('daily_calls',store,self.now)
