import unittest
from unittest.mock import Mock
from datetime import timedelta
from sqlalchemy import select
from tests import test_ema_execution as execution_tests
from trading.fixed_profit_db_journal import DatabaseSessionJournal
from trading.strategy_records import StrategyTrade, StrategyEvent

class DatabaseJournalTests(unittest.TestCase):
    setUp=execution_tests.ExecutionRecoveryTests.setUp
    tearDown=execution_tests.ExecutionRecoveryTests.tearDown
    make_worker=execution_tests.ExecutionRecoveryTests.make_worker

    def journal(self,day=None):
        engine=Mock();engine.connect.return_value.closed=False
        return DatabaseSessionJournal(self.tmp.name,1,day or self.now.date(),live=False,
                                      factory=self.factory,db_engine=engine)

    def test_fill_and_pnl_commit_and_same_day_restart_blocked(self):
        journal=self.journal()
        journal.event('ENTRY_INTENT',side='PE',symbol='TESTPE',expiry='2026-10-27',strike='24000',
                      quantity=65,index='24000',reference_high='24010',reference_low='23990')
        journal.event('ENTRY_FILLED',order_id=None,premium='100',index='24000',stop='24015',target='23970')
        journal.event('EXIT_INTENT',reason='target',index='23970')
        journal.event('EXIT_FILLED',order_id=None,premium='110')
        journal.finish('completed');journal.close()
        with self.factory() as db:
            trade=db.scalar(select(StrategyTrade).where(StrategyTrade.strategy_key=='fixed_profit_filtered'))
            self.assertEqual('closed',trade.status)
            self.assertEqual(650,trade.gross_option_pnl)
            self.assertEqual(30,trade.index_points)
        with self.assertRaisesRegex(RuntimeError,'Prior session'):self.journal()

    def test_committed_reservation_survives_crash_and_blocks_next_day(self):
        journal=self.journal()
        journal.event('ENTRY_INTENT',side='CE',symbol='TESTCE',expiry='2026-10-27',strike='24000',
                      quantity=65,index='24000',reference_high='24010',reference_low='23990')
        journal.close()
        with self.assertRaisesRegex(RuntimeError,'Prior session'):self.journal(self.now.date()+timedelta(days=1))
        with self.factory() as db:
            trade=db.scalar(select(StrategyTrade).where(StrategyTrade.strategy_key=='fixed_profit_filtered'))
            self.assertEqual('entry_pending',trade.status)

    def test_legacy_file_not_silently_abandoned(self):
        from pathlib import Path
        import json
        Path(self.tmp.name,'1-old-paper.json').write_text(json.dumps(dict(date='2026-10-16',events=[dict(action='ENTRY_INTENT')])))
        with self.assertRaisesRegex(RuntimeError,'Legacy'):self.journal()

    def exercise_adapter(self,*,live=False,lose_response=False):
        from types import SimpleNamespace
        from decimal import Decimal
        from trading.fixed_profit_execution import FixedProfitExecutor
        now=self.now
        class Kite:
            VARIETY_REGULAR='regular';PRODUCT_MIS='MIS';ORDER_TYPE_MARKET='MARKET'
            VALIDITY_DAY='DAY';TRANSACTION_TYPE_BUY='BUY';TRANSACTION_TYPE_SELL='SELL'
            def __init__(self):self.sent=[];self.net=[]
            def orders(self):return []
            def positions(self):return {'net':self.net}
            def quote(self,symbols):return {s:dict(last_price=24000 if s=='NSE:NIFTY 50' else 100,timestamp=now) for s in symbols}
            def place_order(self,**kwargs):
                self.sent.append(kwargs)
                if lose_response:raise TimeoutError('response lost')
                return str(len(self.sent))
            def order_history(self,oid):
                order=self.sent[-1]
                self.net=[dict(exchange='NFO',tradingsymbol=order['tradingsymbol'],product='MIS',
                               quantity=order['quantity'] if order['transaction_type']=='BUY' else 0)]
                return [dict(status='COMPLETE',filled_quantity=order['quantity'],average_price=100)]
        # Force journal mode to match the adapter without accessing a broker.
        fake_engine=Mock();fake_engine.connect.return_value.closed=False
        journal=DatabaseSessionJournal(self.tmp.name,1,self.now.date(),live=live,factory=self.factory,db_engine=fake_engine)
        kite=Kite()
        contract=dict(name='NIFTY',instrument_type='PE',expiry=now.date()+timedelta(days=4),
                      strike=24000,tradingsymbol='TESTPE',lot_size=65)
        engine=SimpleNamespace(pending=SimpleNamespace(action='BUY_PE'),reference=SimpleNamespace(high=Decimal('24010'),low=Decimal('23990')),
                               entries=0,confirm_exit=Mock())
        session=SimpleNamespace(engine=engine,qualify_entry=Mock(return_value=True),
                                filter_decision=SimpleNamespace(atr=Decimal('15'),crossings=1))
        executor=FixedProfitExecutor(kite,[contract],journal,live=live,now_fn=lambda:now)
        return kite,journal,session,executor

    def test_paper_adapter_records_database_roundtrip_without_orders(self):
        from types import SimpleNamespace
        kite,journal,session,executor=self.exercise_adapter()
        executor.enter(session)
        executor.exit(SimpleNamespace(reason='session_end'),session.engine)
        self.assertEqual([],kite.sent)
        with self.factory() as db:
            trade=db.scalar(select(StrategyTrade).where(StrategyTrade.strategy_key=='fixed_profit_filtered'))
            self.assertEqual('closed',trade.status)
            self.assertEqual('paper',trade.mode)
        journal.finish('completed');journal.close()

    def test_mock_live_adapter_persists_ids_and_confirms_ownership(self):
        from types import SimpleNamespace
        kite,journal,session,executor=self.exercise_adapter(live=True)
        executor.enter(session)
        executor.exit(SimpleNamespace(reason='target'),session.engine)
        self.assertEqual(2,len(kite.sent))
        with self.factory() as db:
            trade=db.scalar(select(StrategyTrade).where(StrategyTrade.strategy_key=='fixed_profit_filtered'))
            self.assertEqual('closed',trade.status)
            self.assertEqual('1',trade.entry_order_id)
            self.assertEqual('2',trade.exit_order_id)
        journal.finish('completed');journal.close()

    def test_lost_response_leaves_durable_entry_reservation(self):
        from trading.reference_live_execution import UncertainOrder
        kite,journal,session,executor=self.exercise_adapter(live=True,lose_response=True)
        with self.assertRaises(UncertainOrder):executor.enter(session)
        self.assertEqual(1,len(kite.sent))
        with self.factory() as db:
            trade=db.scalar(select(StrategyTrade).where(StrategyTrade.strategy_key=='fixed_profit_filtered'))
            self.assertEqual('entry_pending',trade.status)
        journal.close()
