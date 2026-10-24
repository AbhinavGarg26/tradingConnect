import unittest
from unittest.mock import Mock,patch
from types import SimpleNamespace
from datetime import timedelta
from sqlalchemy import select
from tests.test_ema_execution import ExecutionRecoveryTests
from trading.strategy_records import StrategyEvent,StrategyRun
from trading.strategy_signal_alerts import Subscription,Delivery,enable,dispatch,notification

class SignalAlertTests(unittest.TestCase):
    def setUp(self):
        ExecutionRecoveryTests.setUp(self)
        Subscription.__table__.create(self.engine);Delivery.__table__.create(self.engine)
        enable(self.factory,1,self.now-timedelta(minutes=1))
        with self.factory() as db:
            run=db.get(StrategyRun,self.store.run_id);run.strategy_key='nifty_daily_ema_monthly'
        self.alerter=SimpleNamespace(_enabled=True,bot_token='test',chat_id='test',TELEGRAM_URL='https://example.test/{token}')
        self.response=Mock(status_code=200);self.response.json.return_value={'ok':True}
    tearDown=ExecutionRecoveryTests.tearDown
    make_worker=ExecutionRecoveryTests.make_worker
    def event(self,action='DAILY_EMA_SIGNAL',details=None,when=None,user_id=1):
        with self.factory() as db:
            run=db.get(StrategyRun,self.store.run_id);run.user_id=user_id
            db.add(StrategyEvent(strategy_run_id=run.id,occurred_at=when or self.now,created_at=self.now,
                action=action,reason='test <reason>',details=details or {'decision':'entry','side':'CE'}))
    def test_signal_sent_once_across_dispatch_restarts(self):
        self.event()
        with patch('trading.strategy_signal_alerts.httpx.post',return_value=self.response) as post:
            self.assertEqual(1,dispatch(self.factory,1,self.now,self.alerter)['sent'])
            self.assertEqual(0,dispatch(self.factory,1,self.now,self.alerter)['sent'])
        post.assert_called_once();message=post.call_args.kwargs['json']['text']
        self.assertIn('PAPER · SIGNAL',message);self.assertIn('not confirmed',message)
        self.assertIn('&lt;reason&gt;',message)
    def test_hold_unsubscribed_old_and_other_accounts_are_silent(self):
        self.event(details={'decision':'wait'})
        self.event(when=self.now-timedelta(minutes=20))
        with patch('trading.strategy_signal_alerts.httpx.post') as post:
            dispatch(self.factory,1,self.now,self.alerter)
            self.assertFalse(dispatch(self.factory,2,self.now,self.alerter)['enabled'])
        post.assert_not_called()
        self.event(user_id=2)
        with patch('trading.strategy_signal_alerts.httpx.post') as post:dispatch(self.factory,1,self.now,self.alerter)
        post.assert_not_called()
    def test_timeout_not_resent_and_no_exception_secret_stored(self):
        self.event()
        with patch('trading.strategy_signal_alerts.httpx.post',side_effect=TimeoutError('secret URL')) as post:
            dispatch(self.factory,1,self.now,self.alerter);dispatch(self.factory,1,self.now,self.alerter)
        post.assert_called_once()
        with self.factory() as db:
            row=db.scalar(select(Delivery));self.assertEqual('uncertain',row.status);self.assertEqual('TimeoutError',row.error_code)
    def test_missing_configuration_leaves_event_unclaimed(self):
        self.event();self.alerter._enabled=False
        self.assertFalse(dispatch(self.factory,1,self.now,self.alerter)['configured'])
        with self.factory() as db:self.assertIsNone(db.scalar(select(Delivery)))
    def test_duplicate_source_rows_share_delivery(self):
        self.event();self.event()
        with patch('trading.strategy_signal_alerts.httpx.post',return_value=self.response) as post:dispatch(self.factory,1,self.now,self.alerter)
        post.assert_called_once()
    def test_fills_are_clearly_different_from_signals(self):
        self.event('BUY_CE',{'intent_key':'intent','premium':'100'})
        with patch('trading.strategy_signal_alerts.httpx.post',return_value=self.response) as post:dispatch(self.factory,1,self.now,self.alerter)
        self.assertIn('CONFIRMED FILL',post.call_args.kwargs['json']['text'])
    def test_telegram_rejection_is_recorded_as_failed(self):
        self.event();self.response.json.return_value={'ok':False}
        with patch('trading.strategy_signal_alerts.httpx.post',return_value=self.response):dispatch(self.factory,1,self.now,self.alerter)
        with self.factory() as db:self.assertEqual('failed',db.scalar(select(Delivery)).status)
