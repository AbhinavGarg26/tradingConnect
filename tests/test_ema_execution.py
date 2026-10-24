import os
os.environ.setdefault('DB_ENCRYPTION_KEY','unit-test-key-not-used-for-db')
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date,datetime,timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo
from unittest.mock import patch
from sqlalchemy import create_engine,Integer,JSON,select,func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.dialects.postgresql import JSONB

from trading.strategy_records import StrategyRun,StrategyTrade,StrategyEvent
from trading.strategy_order_intents import StrategyExecutionState,StrategyOrderIntent
from trading.ema_execution import Store,Executor,ReconciliationRequired
from trading.ema_worker import Worker

IST=ZoneInfo('Asia/Kolkata')


def catalog():
    result=[]
    for exp in (date(2026,10,27),date(2026,11,24)):
        result.append(dict(name='NIFTY',instrument_type='FUT',expiry=exp))
        for side in ('CE','PE'):
            result.append(dict(name='NIFTY',instrument_type=side,expiry=exp,strike=24000,
                               lot_size=65,tradingsymbol=f'NIFTY{exp}{side}'))
    return result


def signal(now,*,enter=False,exit=False,valid=True):
    return dict(time=now-timedelta(seconds=2),fast=101,slow=100,bullish=valid,trend_valid=valid,
                enter=enter,exit=exit,eligible_after=now-timedelta(seconds=2),deadline=now+timedelta(seconds=88))


class FakeBroker:
    def __init__(self):self.sent=[];self.fail_on=None;self.manual_change=False
    def spot(self,now):return Decimal('24000')
    def check_entry(self,*args):pass
    def check_exit(self,*args):
        if self.manual_change:raise ReconciliationRequired('Owned lot changed')
    def verify_owned(self,position):
        if position and self.manual_change:raise ReconciliationRequired('Owned lot changed')
    def submit(self,c,side,quantity,tag,now):
        order=dict(order_id=str(len(self.sent)+1),tag=tag,tradingsymbol=c['tradingsymbol'],exchange='NFO',product='NRML',
                   quantity=quantity,filled_quantity=quantity,transaction_type=side,average_price=100,status='COMPLETE')
        self.sent.append(order)
        if len(self.sent)==self.fail_on:raise TimeoutError('broker accepted, response lost')
        return order['order_id']
    def orders(self):return self.sent
    def order(self,oid):return next((o for o in self.sent if o['order_id']==oid),None)


class ExecutionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.engine=create_engine(f'sqlite:///{Path(self.tmp.name)/"execution.sqlite"}')
        self.original_types=[(column,column.type) for model in (StrategyRun,StrategyTrade,StrategyEvent,StrategyExecutionState,StrategyOrderIntent)
                             for column in model.__table__.columns]
        StrategyRun.__table__.c.configuration.type=JSONB().with_variant(JSON(),'sqlite')
        StrategyEvent.__table__.c.details.type=JSONB().with_variant(JSON(),'sqlite')
        for model in (StrategyRun,StrategyTrade,StrategyEvent,StrategyExecutionState,StrategyOrderIntent):
            model.__table__.c.id.type=Integer()
            model.__table__.create(self.engine)
        self.sessions=sessionmaker(self.engine)
        @contextmanager
        def factory():
            with self.sessions.begin() as db:yield db
        self.factory=factory
        self.broker=FakeBroker()
        self.now=datetime(2026,10,19,10,15,2,tzinfo=IST)
        self.config={'kind':'daily_calls','side':'CE','fast':20,'slow':50}
        self.make_worker()

    def tearDown(self):
        self.engine.dispose();self.tmp.cleanup()
        for column,original in self.original_types:column.type=original

    def make_worker(self):
        self.store=Store(self.factory,1,'test_ema','v1','paper',self.config)
        self.store.start(self.now)
        self.executor=Executor(self.store,self.broker,catalog(),lambda d:d.weekday()<5)
        self.worker=Worker(self.executor,self.config.get('side','CE'))

    def trades(self):
        with self.factory() as db:
            return [(t.id,t.status,t.side,t.gross_option_pnl) for t in db.scalars(select(StrategyTrade).order_by(StrategyTrade.id))]

    def test_restart_does_not_repeat_entry_or_exit(self):
        snap=signal(self.now,enter=True)
        self.worker.step(snap,self.now)
        first=self.store.position()['id']
        self.make_worker();self.worker.step(snap,self.now)
        self.assertEqual(1,len(self.broker.sent))
        self.assertEqual(first,self.store.position()['id'])
        later=self.now+timedelta(minutes=15)
        self.worker.step(signal(later,exit=True,valid=False),later)
        self.assertIsNone(self.store.position())
        self.make_worker();self.worker.step(signal(later,exit=True,valid=False),later)
        self.assertEqual(2,len(self.broker.sent))
        self.assertEqual('closed',self.trades()[0][1])

    def test_rejected_exit_remains_halted_after_restart(self):
        self.worker.step(signal(self.now,enter=True),self.now)
        submit=self.broker.submit
        def reject(*args):
            oid=submit(*args)
            self.broker.sent[-1].update(status='REJECTED',filled_quantity=0)
            return oid
        self.broker.submit=reject
        later=self.now+timedelta(minutes=15)
        snap=signal(later,exit=True,valid=False)
        with self.assertRaises(ReconciliationRequired):self.worker.step(snap,later)
        self.assertEqual('halted',self.store.state()['status'])
        self.make_worker()
        with self.assertRaises(ReconciliationRequired):self.worker.step(snap,later)
        self.assertEqual(2,len(self.broker.sent))
        self.assertIsNotNone(self.store.position())

    def test_lost_order_response_reconciles_without_second_submission(self):
        self.broker.fail_on=1;snap=signal(self.now,enter=True)
        with self.assertRaises(TimeoutError):self.worker.step(snap,self.now)
        self.assertEqual(1,len(self.broker.sent))
        self.make_worker();self.worker.step(snap,self.now)
        self.assertEqual(1,len(self.broker.sent))
        self.assertEqual(1,len(self.trades()))
        self.assertIsNotNone(self.store.position())

    def test_crash_after_confirmed_fill_before_trade_application(self):
        snap=signal(self.now,enter=True)
        with patch.object(self.store,'apply_fill',side_effect=RuntimeError('process crashed')):
            with self.assertRaises(RuntimeError):self.worker.step(snap,self.now)
        self.assertEqual([],self.trades())
        self.make_worker();self.worker.step(snap,self.now)
        self.assertEqual(1,len(self.broker.sent))
        self.assertEqual(1,len(self.trades()))
        self.assertIsNotNone(self.store.position())

    def test_partial_fill_stops_and_never_resubmits(self):
        snap=signal(self.now,enter=True)
        submit=self.broker.submit
        def partial(*args):
            oid=submit(*args);self.broker.sent[-1].update(filled_quantity=20,status='OPEN');return oid
        self.broker.submit=partial
        with self.assertRaises(ReconciliationRequired):self.worker.step(snap,self.now)
        self.make_worker()
        with self.assertRaises(ReconciliationRequired):self.worker.step(snap,self.now)
        self.assertEqual(1,len(self.broker.sent))
        self.assertEqual([],self.trades())

    def test_roll_exit_then_entry_and_restart_between_legs(self):
        self.worker.step(signal(self.now,enter=True),self.now)
        first=self.store.position()['id']
        self.now=datetime(2026,10,23,10,15,2,tzinfo=IST)
        self.broker.fail_on=3
        snap=signal(self.now)
        with self.assertRaises(TimeoutError):self.worker.step(snap,self.now)
        self.assertIsNone(self.store.position())
        self.assertEqual('closed',self.trades()[0][1])
        self.make_worker();self.worker.step(snap,self.now)
        self.assertEqual(['BUY','SELL','BUY'],[o['transaction_type'] for o in self.broker.sent])
        self.assertNotEqual(first,self.store.position()['id'])
        self.assertEqual(date(2026,11,24),self.store.position()['expiry'])
        self.assertEqual(2,len(self.trades()))

    def test_changed_broker_position_halts_without_selling(self):
        self.worker.step(signal(self.now,enter=True),self.now)
        self.broker.manual_change=True
        later=self.now+timedelta(minutes=15)
        with self.assertRaises(ReconciliationRequired):self.worker.step(signal(later,exit=True,valid=False),later)
        self.assertEqual(1,len(self.broker.sent))
        self.assertEqual('open',self.trades()[0][1])

    def test_application_transaction_rolls_back_before_marker_then_recovers(self):
        original=self.store.apply_fill
        def crash(key,now):
            with patch('trading.ema_execution.StrategyEvent',side_effect=RuntimeError('event persistence failed')):
                original(key,now)
        snap=signal(self.now,enter=True)
        with patch.object(self.store,'apply_fill',side_effect=crash):
            with self.assertRaises(RuntimeError):self.worker.step(snap,self.now)
        self.assertEqual([],self.trades())
        self.assertIsNone(self.store.position())
        self.make_worker();self.worker.step(snap,self.now)
        self.assertEqual(1,len(self.trades()))
        self.assertEqual(1,len(self.broker.sent))

    def test_bearish_strategy_buys_only_puts_and_exits_to_cash(self):
        self.config={'kind':'bearish_puts','side':'PE','fast':9,'slow':21}
        self.make_worker()
        self.worker.step(signal(self.now,enter=True),self.now)
        self.assertEqual('PE',self.store.position()['side'])
        later=self.now+timedelta(minutes=15)
        self.worker.step(signal(later,exit=True,valid=False),later)
        self.assertIsNone(self.store.position())
        self.assertEqual(['BUY','SELL'],[o['transaction_type'] for o in self.broker.sent])

    def test_paper_receipt_survives_crash_without_calling_broker_order_api(self):
        from unittest.mock import Mock
        from trading.ema_broker import PaperBroker
        kite=Mock()
        def quotes(symbols):
            symbol=symbols[0]
            return {symbol:dict(timestamp=self.now,last_price=24000,depth={
                'sell':[dict(price=450,quantity=65)],'buy':[dict(price=445,quantity=65)]})}
        kite.quote.side_effect=quotes
        self.broker=PaperBroker(kite,self.store)
        self.executor=Executor(self.store,self.broker,catalog(),lambda d:d.weekday()<5)
        self.worker=Worker(self.executor,'CE')
        snap=signal(self.now,enter=True)
        with patch.object(self.store.journal,'broker_submitted',side_effect=RuntimeError('process crashed after paper receipt')):
            with self.assertRaises(RuntimeError):self.worker.step(snap,self.now)
        self.make_worker()
        self.broker=PaperBroker(kite,self.store)
        self.executor=Executor(self.store,self.broker,catalog(),lambda d:d.weekday()<5)
        self.worker=Worker(self.executor,'CE')
        self.worker.step(snap,self.now)
        self.assertEqual(1,len(self.trades()))
        self.assertIsNotNone(self.store.position())
        kite.place_order.assert_not_called()
        kite._post.assert_not_called()

    def test_past_signal_is_not_backfilled(self):
        old=signal(self.now-timedelta(hours=1),enter=True)
        self.worker.step(old,self.now)
        self.assertEqual([],self.broker.sent)
        self.assertIsNone(self.store.position())


if __name__=='__main__':unittest.main()
