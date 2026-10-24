import os
os.environ.setdefault('DB_ENCRYPTION_KEY','unit-test-key-not-used-for-db')
import unittest,tempfile
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
from zoneinfo import ZoneInfo
from sqlalchemy import create_engine,text,select,func,event
from sqlalchemy.orm import sessionmaker
from market.database_candles import read_completed_15m,CandleNotReady
from trading.ema_indicator_cache import IndicatorCacheRow,IndicatorCandleRow

class DatabaseCandlesTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.engine=create_engine('sqlite:///'+str(Path(self.tmp.name,'test.db')))
        IndicatorCacheRow.__table__.create(self.engine);IndicatorCandleRow.__table__.create(self.engine)
        sessions=sessionmaker(self.engine)
        @contextmanager
        def factory():
            with sessions.begin() as db:yield db
        self.factory=factory;self.open=datetime(2026,10,23,9,15,tzinfo=ZoneInfo('Asia/Kolkata'))
        with self.factory() as db:
            db.execute(text('CREATE TABLE market_snapshots (symbol TEXT,timeframe TEXT,captured_at TIMESTAMP,open_price NUMERIC,high_price NUMERIC,low_price NUMERIC,close_price NUMERIC,ltp NUMERIC,created_at TIMESTAMP,updated_at TIMESTAMP,UNIQUE(symbol,timeframe,captured_at))'))
            for i in range(30):
                stamp=(self.open+timedelta(minutes=i)).astimezone(timezone.utc).replace(tzinfo=None)
                db.execute(text("INSERT INTO market_snapshots (symbol,timeframe,captured_at,open_price,high_price,low_price,close_price) VALUES ('NIFTY 50','1m',:stamp,:o,:h,:l,:c)"),dict(stamp=stamp,o=100+i,h=102+i,l=99+i,c=101+i))
    def tearDown(self):self.engine.dispose();self.tmp.cleanup()
    def read(self,minutes=30,**kwargs):return read_completed_15m(self.open+timedelta(minutes=minutes,seconds=5),factory=self.factory,**kwargs)
    def count(self):
        with self.factory() as db:return db.scalar(select(func.count()).select_from(IndicatorCandleRow))
    def test_exact_session_aggregation_and_cache_reuse_without_raw_reads(self):
        bars=self.read();self.assertEqual(2,len(bars));self.assertEqual(100,bars[0]['open']);self.assertEqual(116,bars[0]['high']);self.assertEqual(99,bars[0]['low']);self.assertEqual(115,bars[0]['close'])
        statements=[]
        def observe(conn,cursor,statement,parameters,context,executemany):statements.append(statement)
        event.listen(self.engine,'before_cursor_execute',observe)
        self.assertEqual(bars,self.read());self.assertFalse(any('FROM market_snapshots' in s for s in statements));self.assertEqual(2,self.count())
    def test_only_new_bucket_and_no_lookahead(self):
        first=self.read(15);self.assertEqual(1,len(first));self.assertEqual(1,self.count())
        second=self.read(after=first[0]['date']);self.assertEqual(1,len(second));self.assertEqual(self.open+timedelta(minutes=15),second[0]['date']);self.assertEqual(2,self.count())
        self.assertEqual([],read_completed_15m(self.open+timedelta(minutes=15,seconds=4),factory=self.factory))
    def test_missing_minute_aborts_all_publication(self):
        with self.factory() as db:db.execute(text('DELETE FROM market_snapshots WHERE close_price=120'))
        with self.assertRaises(CandleNotReady):self.read()
        self.assertEqual(0,self.count())
    def test_readiness_does_not_write(self):
        self.assertEqual(2,len(self.read(persist=False)));self.assertEqual(0,self.count())
        with self.factory() as db:self.assertIsNone(db.scalar(select(IndicatorCacheRow)))
    def test_invalid_prices_block(self):
        with self.factory() as db:db.execute(text('UPDATE market_snapshots SET high_price=1 WHERE close_price=120'))
        with self.assertRaises(CandleNotReady):self.read()
        self.assertEqual(0,self.count())
