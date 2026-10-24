import os
os.environ.setdefault('DB_ENCRYPTION_KEY','unit-test-key-not-used-for-db')
import unittest
from unittest.mock import Mock,patch
import nifty_15m_live as runner
from deploy.strategy_unit_control import command_for
from strategy_service_status import observe
from strategy_control_worker import validate_request
from datetime import datetime,timezone

class BreakoutControlTests(unittest.TestCase):
    def test_profile_preserves_token_retry_options_and_mode(self):
        command=command_for('nifty_15m_breakout','live',1,'/app')
        self.assertIn('nifty_15m_live.py --user-id 1',command)
        self.assertIn('--token-attempts 0 --token-retry-seconds 300',command)
        self.assertTrue(command.endswith('--live'))
        self.assertNotIn('--live',command_for('nifty_15m_breakout','paper',1,'/app'))
        self.assertNotIn('--no-risk-cap',command)

    def test_observer_uses_existing_unit(self):
        calls=[]
        def probe(unit):
            calls.append(unit)
            return dict(LoadState='loaded',ActiveState='active',ExecStart='/app/nifty_15m_live.py --user-id 1 --live')
        result=observe('nifty_15m_breakout',probe,expected_user_id=1)
        self.assertEqual('live',result['configured_mode'])
        self.assertEqual(['nifty-15m-v3.service','nifty-15m-v3.timer'],calls)

    def test_legacy_account_selection_is_not_claimed_as_verified(self):
        report=observe('nifty_15m_breakout',lambda unit:dict(ExecStart='/app/nifty_15m_live.py --live'),expected_user_id=1)
        self.assertEqual('unknown',report['configured_mode'])
        self.assertIn('account binding',report['command_issue'])

    def test_start_accepts_later_window_without_ema_journal_queries(self):
        now=datetime(2026,10,23,6,30,tzinfo=timezone.utc) # noon IST
        row=dict(user_id=1,strategy_key='nifty_15m_breakout',operation='start',mode='paper',created_at=now,risk_acknowledged=False)
        db=Mock();db.execute.return_value.first.return_value=None
        validate_request(row,db,now)
        self.assertFalse(any('strategy_order_intents' in str(call.args[0]) for call in db.execute.call_args_list))
        now=now.replace(hour=10) # 15:30 IST
        row['created_at']=now
        with self.assertRaisesRegex(ValueError,'startup window'):validate_request(row,db,now)

    def test_runner_uses_explicit_account_without_sending_orders(self):
        for live in (False,True):
            with self.subTest(live=live):
                argv=['nifty_15m_live.py','--user-id','1']+(['--live'] if live else [])
                with patch('sys.argv',argv), patch('nifty_15m_env.load_nifty_environment'), \
                     patch('trading.user_token.fetch_user_token',return_value=(Mock(),1)) as fetch, \
                     patch('trading.service_runtime.ServiceRuntimeMonitor'), \
                     patch('market.trading_calendar.is_nse_trading_day',return_value=True), \
                     patch.object(runner,'run') as run, \
                     patch.object(runner,'connect_live_with_token_retry',side_effect=lambda fetch,**kw:fetch(Mock())):
                    runner.main()
                    self.assertEqual(1,fetch.call_args.kwargs['user_id'])
                    self.assertEqual(live,run.call_args.kwargs['live'])
