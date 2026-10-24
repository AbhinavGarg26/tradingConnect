import unittest
from contextlib import contextmanager
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.test_nifty_30m_live import FakeKite, Engine, NOW, catalog, signal
from trading.reference_protected_execution import ProtectedReferenceExecutor, place_reference_stop
from trading.reference_live_execution import OwnedPosition, UncertainOrder
from trading.strategy_risk import RiskBlocked


@contextmanager
def owned(_):
    yield True


@contextmanager
def busy(_):
    yield False


class ProtectedExecutionTest(unittest.TestCase):
    def setUp(self):
        self.kite=FakeKite()
        self.journal=Mock(user_id=1,trade_id=7)
        self.engine=Engine()
        self.executor=ProtectedReferenceExecutor(self.kite,catalog(),self.journal,live=True,now_fn=lambda:NOW)
        self.limits=SimpleNamespace(risk_per_trade=D(1000),stop_limit_buffer_pct=D(3),amount_allocated=D(15000))
        self.contract=catalog()[42]  # exact symbol is irrelevant to ownership checks
        self.trade=SimpleNamespace(id=7,status='open',strategy_key='nifty_30m_reference',protection_order_id='stop7')
        self.journal.trade_snapshot.return_value=self.trade

    def test_risk_disabled_blocks_before_order_and_journal_reservation(self):
        with patch('trading.reference_protected_execution.check_new_entry',side_effect=RiskBlocked('disabled')):
            with self.assertRaises(RiskBlocked):self.executor.handle(signal(),self.engine)
        self.assertEqual(self.kite.sent,[])
        self.journal.begin_entry.assert_not_called()

    def test_stale_guardian_blocks_before_order(self):
        with patch('trading.reference_protected_execution.check_new_entry',side_effect=RiskBlocked('guardian stale')):
            with self.assertRaises(RiskBlocked):self.executor.handle(signal(),self.engine)
        self.assertEqual(self.kite.sent,[])

    def test_live_fill_gets_protection_before_postfill_index_request(self):
        calls=[]
        with patch('trading.reference_protected_execution.check_new_entry',return_value=self.limits), \
             patch('trading.reference_protected_execution.trade_execution_lock',owned), \
             patch('trading.reference_protected_execution.place_reference_stop',side_effect=lambda *a:calls.append('stop')):
            self.journal.set_entry_index.side_effect=lambda *a:calls.append('index')
            self.executor.handle(signal(),self.engine)
        self.assertEqual(calls,['stop','index'])
        self.assertEqual(self.kite.sent[0]['transaction_type'],'BUY')

    def test_protection_failure_attempts_emergency_and_never_paper_fallback(self):
        with patch('trading.reference_protected_execution.check_new_entry',return_value=self.limits), \
             patch('trading.reference_protected_execution.trade_execution_lock',owned), \
             patch('trading.reference_protected_execution.place_reference_stop',side_effect=TimeoutError()), \
             patch('strategy_guardian.emergency_exit') as emergency:
            with self.assertRaises(UncertainOrder):self.executor.handle(signal(),self.engine)
        emergency.assert_called_once()
        self.assertFalse(self.executor.position.paper)
        self.assertEqual(len(self.kite.sent),1)

    def test_margin_rejection_fallback_remains_paper_without_stop(self):
        self.kite.mode='funds_rejected'
        with patch('trading.reference_protected_execution.check_new_entry',return_value=self.limits), \
             patch('trading.reference_protected_execution.place_reference_stop') as stop:
            self.executor.handle(signal(),self.engine)
        self.assertTrue(self.executor.position.paper)
        stop.assert_not_called()

    def prepare_exit(self):
        self.executor.position=OwnedPosition(self.contract,65,False)
        self.kite.net=[dict(exchange='NFO',tradingsymbol=self.contract['tradingsymbol'],quantity=65,product='MIS')]

    def test_guardian_lock_prevents_duplicate_sell(self):
        self.prepare_exit()
        with patch('trading.reference_protected_execution.trade_execution_lock',busy):
            self.executor.handle(signal('EXIT_CE'),self.engine)
        self.assertEqual(self.kite.sent,[])
        self.journal.exit_intent.assert_not_called()

    def test_confirmed_stop_fill_never_sends_additional_sell(self):
        self.prepare_exit()
        with patch('trading.reference_protected_execution.trade_execution_lock',owned), \
             patch('strategy_guardian.cancel_protection',return_value={'average_price':100}), \
             patch('strategy_guardian.finish_exit') as finish:
            self.executor.handle(signal('EXIT_CE'),self.engine)
        finish.assert_called_once()
        self.assertEqual(self.kite.sent,[])
        self.assertIsNone(self.executor.position)

    def test_normal_exit_cancels_stop_before_market_sell(self):
        self.prepare_exit();events=[]
        self.journal.exit_intent.side_effect=lambda *a,**k:events.append('reserve')
        with patch('trading.reference_protected_execution.trade_execution_lock',owned), \
             patch('strategy_guardian.cancel_protection',side_effect=lambda *a:events.append('cancel')):
            self.executor.handle(signal('EXIT_CE'),self.engine)
        self.assertEqual(events,['reserve','cancel'])
        self.assertEqual(len(self.kite.sent),1)
        self.assertEqual(self.kite.sent[0]['transaction_type'],'SELL')

    def test_cancellation_uncertainty_does_not_sell(self):
        self.prepare_exit()
        with patch('trading.reference_protected_execution.trade_execution_lock',owned), \
             patch('strategy_guardian.cancel_protection',side_effect=RuntimeError('unconfirmed')):
            with self.assertRaises(RuntimeError):self.executor.handle(signal('EXIT_CE'),self.engine)
        self.assertEqual(self.kite.sent,[])

    def test_closed_guardian_trade_is_reconciled_locally(self):
        self.prepare_exit();self.trade.status='closed'
        self.assertFalse(self.executor.synchronize(self.engine))
        self.assertIsNone(self.executor.position)
        self.journal.release_closed_trade.assert_called_once()
        self.assertEqual(self.kite.sent,[])

    def test_guardian_pending_exit_pauses_runner(self):
        self.prepare_exit();self.trade.status='exit_pending'
        self.assertTrue(self.executor.synchronize(self.engine))
        self.assertEqual(self.kite.sent,[])

    def test_changed_manual_quantity_does_not_cancel_or_sell(self):
        self.prepare_exit();self.kite.net[0]['quantity']=130
        self.kite.order_history=lambda oid:[dict(status='TRIGGER PENDING',filled_quantity=0)]
        with patch('trading.reference_protected_execution.trade_execution_lock',owned), \
             patch('strategy_guardian.cancel_protection') as cancel:
            with self.assertRaises(UncertainOrder):self.executor.handle(signal('EXIT_CE'),self.engine)
        cancel.assert_not_called()
        self.assertEqual(self.kite.sent,[])

    def test_actual_allocation_breach_attempts_emergency_exit(self):
        limits=SimpleNamespace(risk_per_trade=D(1000),stop_limit_buffer_pct=D(3),amount_allocated=D(1))
        with patch('trading.reference_protected_execution.check_new_entry',return_value=limits), \
             patch('trading.reference_protected_execution.trade_execution_lock',owned), \
             patch('trading.reference_protected_execution.place_reference_stop'), \
             patch('strategy_guardian.emergency_exit') as emergency:
            with self.assertRaises(UncertainOrder):self.executor.handle(signal(),self.engine)
        emergency.assert_called_once()
        self.assertEqual(len(self.kite.sent),1)

    def test_paper_mode_needs_neither_risk_settings_nor_protection(self):
        self.executor.live=False
        with patch('trading.reference_protected_execution.check_new_entry') as gate, \
             patch('trading.reference_protected_execution.place_reference_stop') as stop:
            self.executor.handle(signal(),self.engine)
            self.executor.handle(signal('EXIT_CE'),self.engine)
        gate.assert_not_called();stop.assert_not_called()
        self.assertEqual(self.kite.sent,[])

    def test_runner_exception_leaves_guardian_exit_owner_intact(self):
        from trading.reference_live_records import ReferenceRecorder
        from contextlib import contextmanager
        trade=SimpleNamespace(status='exit_pending',exit_reason='hard_daily_loss')
        @contextmanager
        def session():
            db=Mock();db.get.return_value=trade
            yield db
        recorder=ReferenceRecorder.__new__(ReferenceRecorder);recorder.trade_id=7
        with patch('trading.reference_live_records.get_db',session):recorder.uncertain('runner stopped')
        self.assertEqual(trade.status,'exit_pending')

    def test_reference_guardian_uses_same_lock(self):
        from strategy_guardian import emergency_exit
        with patch('trading.trade_execution_lock.trade_execution_lock',busy), \
             patch('strategy_guardian.reserve_exit') as reserve:
            emergency_exit(self.kite,self.trade,'risk')
        reserve.assert_not_called()
        self.assertEqual(self.kite.sent,[])

    def test_broker_stop_parameters_are_option_premium_based(self):
        self.kite.ORDER_TYPE_SL='SL'
        self.kite.order_history=lambda oid:[dict(status='TRIGGER PENDING',filled_quantity=0)]
        place_reference_stop(self.kite,self.contract,65,D(120),self.limits,self.journal)
        sent=self.kite.sent[0]
        self.assertEqual(sent['order_type'],'SL')
        self.assertEqual(sent['transaction_type'],'SELL')
        self.assertEqual(sent['tag'],'NIFTY30STOP')
        self.assertLess(sent['price'],sent['trigger_price'])
        self.journal.protection_submitted.assert_called_once()


if __name__=='__main__':unittest.main()
