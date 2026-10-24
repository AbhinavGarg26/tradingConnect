import os
os.environ.setdefault('DB_ENCRYPTION_KEY','unit-test-key-not-used-for-db')
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock
from deploy.strategy_unit_control import apply,command_for
from strategy_control_worker import validate_request,process_one

CONFIG=dict(user_id=1,project_dir='/home/deployer/apps/tradingConnect',service_user='deployer')


class HelperTests(unittest.TestCase):
    def setUp(self):
        self.calls=[];self.writes=[]
    def ctl(self,*args):
        self.calls.append(args)
        if '--property=LoadState' in args:return 'loaded'
        if '--property=User' in args:return 'deployer'
        if '--property=ActiveState' in args:return 'inactive'
        if '--property=Job' in args:return ''
        if '--property=ExecStart' in args:return 'argv[]='+self.writes[-1][1]+' ;'
        return ''
    def write(self,*args):self.writes.append(args)
    def test_schedule_live_ema_uses_exact_profile_and_ack_flag(self):
        result=apply(CONFIG,1,'nifty_bearish_15m_9_21','schedule','live',self.ctl,self.write)
        self.assertIn('LIVE',result)
        self.assertIn('--strategy bearish_puts --mode live',self.writes[0][1])
        self.assertIn('--acknowledge-premium-risk',self.writes[0][1])
        self.assertIn(('enable','--now','nifty-bearish-15m-ema.timer'),self.calls)
        self.assertFalse(any(c[0]=='start' for c in self.calls))
    def test_pause_never_stops_runner_or_changes_command(self):
        apply(CONFIG,1,'fixed_profit_filtered','pause','paper',self.ctl,self.write)
        self.assertEqual([],self.writes)
        self.assertEqual(('disable','--now','fixed-profit-filtered.timer'),self.calls[-1])
    def test_account_and_command_injection_rejected_without_effects(self):
        for user,key in [(2,'fixed_profit_filtered'),(1,'fixed_profit_filtered; reboot')]:
            with self.assertRaises(ValueError):apply(CONFIG,user,key,'start','live',self.ctl,self.write)
        self.assertEqual([],self.calls)
    def test_active_runner_refuses_mode_change(self):
        def active(*args):
            if '--property=ActiveState' in args:return 'active'
            return self.ctl(*args)
        with self.assertRaisesRegex(RuntimeError,'Runner is active'):
            apply(CONFIG,1,'fixed_profit_filtered','start','live',active,self.write)
        self.assertEqual([],self.writes)
    def test_start_does_not_enable_recurring_timer(self):
        apply(CONFIG,1,'fixed_profit_filtered','start','paper',self.ctl,self.write)
        self.assertIn(('start','--no-block','fixed-profit-filtered.service'),self.calls)
        self.assertNotIn(('enable','--now','fixed-profit-filtered.timer'),self.calls)
        self.assertNotIn('--live',self.writes[0][1])
    def test_root_service_refused(self):
        def root(*args):
            if '--property=User' in args:return 'root'
            return self.ctl(*args)
        with self.assertRaisesRegex(RuntimeError,'unprivileged'):
            apply(CONFIG,1,'fixed_profit_filtered','start','paper',root,self.write)
        self.assertEqual([],self.writes)
    def test_pe1_retains_its_preset_and_guardian_timer(self):
        apply(CONFIG,1,'nifty_30m_reference','schedule','live',self.ctl,self.write)
        self.assertIn('--preset pe1-v1 --live',self.writes[0][1])
        self.assertIn(('enable','--now','strategy-guardian.timer'),self.calls)
    def test_pending_job_blocks_override(self):
        def pending(*args):
            if '--property=Job' in args:return '42 /job/42'
            return self.ctl(*args)
        with self.assertRaisesRegex(RuntimeError,'job is pending'):
            apply(CONFIG,1,'fixed_profit_filtered','schedule','paper',pending,self.write)
        self.assertEqual([],self.writes)

    def test_later_override_cannot_silently_change_selected_mode(self):
        def wrong(*args):
            if '--property=ExecStart' in args:return 'argv[]='+self.writes[-1][1]+' --live ;'
            return self.ctl(*args)
        with self.assertRaisesRegex(RuntimeError,'Another service override'):
            apply(CONFIG,1,'fixed_profit_filtered','schedule','paper',wrong,self.write)
        self.assertNotIn(('enable','--now','fixed-profit-filtered.timer'),self.calls)


class RequestValidationTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,10,23,3,50,tzinfo=timezone.utc)
        self.row=dict(id=1,user_id=1,strategy_key='fixed_profit_filtered',operation='schedule',mode='live',
                      risk_acknowledged=True,created_at=self.now)
        self.db=Mock();self.db.execute.return_value.first.return_value=None
    def test_live_requires_acknowledgement(self):
        self.row['risk_acknowledged']=False
        with self.assertRaisesRegex(ValueError,'acknowledgement'):validate_request(self.row,self.db,self.now)
    def test_expired_request_cannot_start_live_later(self):
        self.row['created_at']=self.now-timedelta(minutes=6)
        with self.assertRaisesRegex(ValueError,'expired'):validate_request(self.row,self.db,self.now)
    def test_open_position_blocks_mode_changes(self):
        self.db.execute.return_value.first.return_value=(123,)
        with self.assertRaisesRegex(ValueError,'position'):validate_request(self.row,self.db,self.now)
    def test_pause_remains_possible_with_position(self):
        self.row['operation']='pause'
        self.db.execute.return_value.first.return_value=(123,)
        validate_request(self.row,self.db,self.now)
    def test_start_outside_window_requires_schedule(self):
        self.row['operation']='start'
        self.row['created_at']=self.now.replace(hour=8)
        with self.assertRaisesRegex(ValueError,'startup window'):
            validate_request(self.row,self.db,self.now.replace(hour=8))
    def test_naive_rails_timestamp_is_treated_as_utc(self):
        self.row['created_at']=self.now.replace(tzinfo=None)
        validate_request(self.row,self.db,self.now)
    def test_live_pe1_requires_saved_risk_approval(self):
        self.row['strategy_key']='nifty_30m_reference'
        self.db.execute.return_value.mappings.return_value.first.return_value=None
        with self.assertRaisesRegex(ValueError,'risk settings'):validate_request(self.row,self.db,self.now)


class QueueTests(unittest.TestCase):
    def test_claim_commits_before_helper_and_failure_is_not_retried(self):
        now=datetime.now(timezone.utc)
        record=dict(id=1,user_id=1,strategy_key='fixed_profit_filtered',operation='schedule',mode='paper',
                    risk_acknowledged=False,created_at=now,status='queued')
        committed=[]
        class DB:
            def execute(self,query,params):
                sql=str(query);result=Mock()
                result.first.return_value=None
                result.mappings.return_value.first.return_value=None
                if sql.startswith('SELECT * FROM strategy_control_requests'):
                    result.mappings.return_value.first.return_value=record.copy() if record['status']=='queued' else None
                elif "SET status='processing'" in sql:record['status']='processing'
                elif sql.startswith('UPDATE strategy_control_requests SET status=:status'):record.update(status=params['status'])
                return result
        @contextmanager
        def factory():
            yield DB()
            committed.append(record['status'])
        calls=[]
        def lost(row):
            self.assertIn('processing',committed)
            calls.append(row['id'])
            raise TimeoutError('helper response lost')
        with self.assertLogs('strategy_control_worker',level='ERROR'):
            process_one(factory,1,lost,lambda:now)
        self.assertEqual('uncertain',record['status'])
        self.assertFalse(process_one(factory,1,lost,lambda:now))
        self.assertEqual([1],calls)
