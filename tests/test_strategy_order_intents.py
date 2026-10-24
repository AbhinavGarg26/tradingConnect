import os
os.environ.setdefault('DB_ENCRYPTION_KEY','unit-test-key-not-used-for-db')
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime,timezone,timedelta
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from trading.strategy_order_intents import IntentJournal,StrategyOrderIntent,StrategyExecutionState,event_key


class IntentJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.engine=create_engine(f'sqlite:///{Path(self.tmp.name)/"journal.sqlite"}')
        StrategyOrderIntent.__table__.create(self.engine)
        StrategyExecutionState.__table__.create(self.engine)
        self.sessions=sessionmaker(self.engine)
        @contextmanager
        def factory():
            with self.sessions.begin() as db: yield db
        self.factory=factory
        self.journal=IntentJournal(factory)
        self.args=dict(user_id=1,strategy_key='test',strategy_version='v1',mode='paper',
            signal_time=datetime(2026,10,23,9,30,tzinfo=timezone.utc),action='entry',
            contract={'tradingsymbol':'TESTPE','exchange':'NFO','product':'NRML'},quantity=65)

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()

    def test_checkpoint_restores_position_and_rejects_stale_worker(self):
        args=dict(user_id=1,strategy_key='test',strategy_version='v1',mode='paper',expected_revision=0,
                  candle_time=self.args['signal_time'],position_trade_id=7,pending_signal={'action':'exit'},
                  configuration={'fast':9,'slow':21},status='position_open')
        self.assertEqual(1,self.journal.save_checkpoint(**args))
        restarted=IntentJournal(self.factory)
        state=restarted.load_state(user_id=1,strategy_key='test',mode='paper')
        self.assertEqual(7,state['position_trade_id'])
        self.assertEqual({'action':'exit'},state['pending_signal'])
        with self.assertRaisesRegex(RuntimeError,'another worker'):
            restarted.save_checkpoint(**args)
        with self.assertRaisesRegex(RuntimeError,'configuration'):
            restarted.save_checkpoint(**(args|{'expected_revision':1,'strategy_version':'v2'}))
        with self.assertRaisesRegex(RuntimeError,'backwards'):
            restarted.save_checkpoint(**(args|{'expected_revision':1,'candle_time':args['candle_time']-timedelta(minutes=15)}))

    def test_restart_reuses_one_intent_and_only_one_submission_claim(self):
        a=self.journal.reserve(**self.args)
        restarted=IntentJournal(self.factory)
        b=restarted.reserve(**self.args)
        self.assertEqual(a['id'],b['id'])
        self.assertTrue(self.journal.claim_submission(a['idempotency_key']))
        self.assertFalse(restarted.claim_submission(a['idempotency_key']))
        pending=restarted.recovery(user_id=1,strategy_key='test',mode='paper')
        self.assertEqual(1,len(pending))
        self.assertEqual('submitting',pending[0]['status'])
        self.assertIsNone(pending[0]['broker_order_id'])

    def test_same_signal_cannot_change_contract_quantity_or_version(self):
        self.journal.reserve(**self.args)
        for changes in ({'quantity':130},{'strategy_version':'v2'},{'contract':{'tradingsymbol':'OTHER'}}):
            with self.assertRaises(RuntimeError): self.journal.reserve(**(self.args|changes))

    def test_unknown_submission_halts_until_matching_broker_order_found(self):
        a=self.journal.reserve(**self.args); key=a['idempotency_key']
        self.journal.claim_submission(key)
        self.journal.uncertain(key,'process lost broker response')
        self.assertFalse(self.journal.claim_submission(key))
        self.assertFalse(self.journal.attach_reconciled_order(key,[]))
        record=dict(tag=a['client_tag'],tradingsymbol='TESTPE',quantity=65,
                    transaction_type='BUY',exchange='NFO',product='NRML',order_id='broker-1')
        with self.assertRaises(RuntimeError):
            self.journal.attach_reconciled_order(key,[record,record])
        self.assertTrue(self.journal.attach_reconciled_order(key,[record]))
        self.journal.confirmed_fill(key,filled_quantity=65,average_price=100)
        self.assertEqual([],self.journal.recovery(user_id=1,strategy_key='test',mode='paper'))
        self.assertFalse(self.journal.claim_submission(key))

    def test_partial_fill_is_uncertain_never_retries_whole_lot(self):
        a=self.journal.reserve(**self.args); key=a['idempotency_key']
        self.journal.claim_submission(key)
        self.journal.broker_submitted(key,'broker-1')
        self.journal.confirmed_fill(key,filled_quantity=20,average_price=100)
        rows=self.journal.recovery(user_id=1,strategy_key='test',mode='paper')
        self.assertEqual('uncertain',rows[0]['status'])
        self.assertEqual(20,rows[0]['filled_quantity'])
        self.assertFalse(self.journal.claim_submission(key))

    def test_key_is_timezone_invariant_and_scoped_by_account_strategy_mode_action(self):
        t=self.args['signal_time']
        base=event_key(1,'test','paper',t,'entry')
        local=t.astimezone(timezone(timedelta(hours=5,minutes=30)))
        self.assertEqual(base,event_key(1,'test','paper',local,'entry'))
        self.assertNotEqual(base,event_key(2,'test','paper',t,'entry'))
        self.assertNotEqual(base,event_key(1,'other','paper',t,'entry'))
        self.assertNotEqual(base,event_key(1,'test','live',t,'entry'))
        self.assertNotEqual(base,event_key(1,'test','paper',t,'exit'))
        self.assertEqual(20,len(self.journal.reserve(**self.args)['client_tag']))

    def test_unresolved_old_signal_blocks_even_a_different_new_signal(self):
        first=self.journal.reserve(**self.args)
        self.journal.claim_submission(first['idempotency_key'])
        later=self.journal.reserve(**(self.args|{'signal_time':self.args['signal_time']+timedelta(minutes=15)}))
        with self.assertRaisesRegex(RuntimeError,'unresolved'):
            self.journal.claim_submission(later['idempotency_key'])

    def test_roll_has_separate_persistent_exit_and_entry_legs(self):
        a=self.journal.reserve(**(self.args|{'action':'roll_exit'}))
        b=self.journal.reserve(**(self.args|{'action':'roll_entry'}))
        self.assertNotEqual(a['idempotency_key'],b['idempotency_key'])
        self.assertEqual(a['id'],self.journal.reserve(**(self.args|{'action':'roll_exit'}))['id'])


if __name__=='__main__':
    unittest.main()
