import os
os.environ.setdefault('DB_ENCRYPTION_KEY','unit-test-key-not-used-for-db')
import unittest
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from market.candle_polling import ClosedCandlePoller,is_rate_limit

class CandlePollingTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,10,23,10,30,5,tzinfo=ZoneInfo('Asia/Kolkata'))
        self.reader=ClosedCandlePoller()
    def test_one_read_per_close_not_every_five_seconds(self):
        self.assertTrue(self.reader.due(self.now));self.reader.success(self.now)
        for seconds in range(5,900,5):self.assertFalse(self.reader.due(self.now+timedelta(seconds=seconds)))
        self.assertTrue(self.reader.due(self.now+timedelta(minutes=15)))
    def test_first_bar_waits_for_finalisation(self):
        self.assertFalse(self.reader.due(self.now.replace(hour=9,minute=30,second=4)))
        self.assertTrue(self.reader.due(self.now.replace(hour=9,minute=30,second=5)))
    def test_rate_limit_has_bounded_backoff_and_recovers_same_slot(self):
        stamp=self.now
        for delay in (10,20,40):
            self.assertEqual(delay,self.reader.rate_limited(stamp))
            self.assertFalse(self.reader.due(stamp+timedelta(seconds=delay-1)))
            stamp+=timedelta(seconds=delay)
            self.assertTrue(self.reader.due(stamp))
        self.reader.success(stamp)
        self.assertFalse(self.reader.due(stamp+timedelta(seconds=5)))
    def test_long_outage_halts_without_marking_candle_processed(self):
        self.reader.rate_limited(self.now)
        with self.assertRaisesRegex(RuntimeError,'stale signals'):
            self.reader.rate_limited(self.now+timedelta(seconds=90))
        self.assertIsNone(self.reader.last_success)
    def test_only_explicit_http_429_is_retried(self):
        from types import SimpleNamespace
        self.assertTrue(is_rate_limit(SimpleNamespace(code=429)))
        self.assertFalse(is_rate_limit(SimpleNamespace(code=403)))
        self.assertFalse(is_rate_limit(TimeoutError('unconfirmed order')))

    def test_runner_waits_for_database_candle_without_broker_history(self):
        import os
        os.environ.setdefault('DB_ENCRYPTION_KEY','unit-test-key-not-used-for-db')
        from unittest.mock import Mock,patch
        from market.database_candles import CandleNotReady
        import nifty_15m_live as runner
        class EndTest(Exception):pass
        stamps=[self.now+timedelta(seconds=5*i) for i in range(5)]
        cursor=[0]
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return stamps[cursor[0]]
        def sleep(seconds):
            cursor[0]+=1
            if cursor[0]>=len(stamps):raise EndTest()
        kite=Mock()
        kite.instruments.side_effect=lambda exchange:([dict(tradingsymbol='NIFTY 50',instrument_token=1)] if exchange=='NSE' else [dict(name='NIFTY',instrument_type='CE',expiry=self.now.date()+timedelta(days=4))])
        records=[CandleNotReady('database not ready'),
            [dict(date=self.now.replace(minute=15,second=0),open=100,high=102,low=99,close=101)]]
        with patch.object(runner,'datetime',Clock),patch.object(runner.time,'sleep',sleep), patch.object(runner,'read_completed_15m',side_effect=records) as reader:
            with self.assertRaises(EndTest):runner._run_session(kite,recorder=Mock())
        self.assertEqual(2,reader.call_count)
        kite.historical_data.assert_not_called()
        kite.place_order.assert_not_called()

    def test_runner_import_does_not_require_database_secrets_before_env_load(self):
        import subprocess,sys
        from pathlib import Path
        root=str(Path(__file__).resolve().parents[1])
        environment=dict(os.environ)
        environment.pop('DB_ENCRYPTION_KEY',None)
        environment['PYTHONPATH']=root
        result=subprocess.run([sys.executable,'-c','import nifty_15m_live'],cwd='/tmp',env=environment,capture_output=True,text=True)
        self.assertEqual(0,result.returncode,result.stderr)

    def test_live_risk_gate_uses_recorders_account_before_any_order(self):
        from unittest.mock import Mock,patch
        from decimal import Decimal
        from trading.strategy_risk import RiskBlocked
        import nifty_15m_live as runner
        stamps=[self.now.replace(hour=9,minute=30),self.now.replace(hour=9,minute=45)]
        cursor=[0]
        class EndTest(Exception):pass
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return stamps[cursor[0]]
        def sleep(seconds):
            cursor[0]+=1
            if cursor[0]==len(stamps):raise EndTest()
        kite=Mock()
        kite.instruments.side_effect=lambda exchange:([dict(tradingsymbol='NIFTY 50',instrument_token=1)] if exchange=='NSE' else [dict(name='NIFTY',instrument_type='CE',expiry=self.now.date()+timedelta(days=4))])
        kite.orders.return_value=[]
        first=dict(date=self.now.replace(hour=9,minute=15,second=0),open=100,high=102,low=99,close=101)
        second=dict(date=self.now.replace(hour=9,minute=30,second=0),open=101,high=105,low=100,close=104)
        records=[[first],[second]]
        kite.quote.return_value={'NFO:TESTCE':dict(timestamp=Clock(2026,10,23,9,45,5,tzinfo=self.now.tzinfo),depth={'sell':[dict(price=Decimal('100'))]})}
        recorder=Mock(user_id=975447485,trade_id=None)
        contract=dict(tradingsymbol='TESTCE',lot_size=65)
        with patch.object(runner,'datetime',Clock),patch.object(runner.time,'sleep',sleep), \
             patch.object(runner,'atm_option',return_value=contract), patch.object(runner,'read_completed_15m',side_effect=records), \
             patch('trading.strategy_risk.check_new_entry',side_effect=RiskBlocked('test blocks entry')) as gate:
            with self.assertRaises(EndTest):runner._run_session(kite,recorder=recorder,live=True)
        self.assertEqual(975447485,gate.call_args.args[0])
        kite.place_order.assert_not_called()
