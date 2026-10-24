import unittest
from strategy_service_status import observe

class StatusTests(unittest.TestCase):
    def test_systemd_environment_user_id_is_resolved_without_storing_secrets(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory,'strategy.env');source.write_text('EMA_USER_ID=1\nOTHER_SECRET=never-persist\n')
            report=observe('fixed_profit_filtered',lambda unit:dict(
                ExecStart='/app/fixed_profit_filtered_live.py --user-id ${EMA_USER_ID}',
                EnvironmentFiles=f'{source} (ignore_errors=no)'),expected_user_id=1)
            self.assertEqual('paper',report['configured_mode'])
            self.assertNotIn('never-persist',str(report))

    def test_missing_units_do_not_claim_paper_or_scheduled(self):
        report=observe('fixed_profit_filtered',lambda unit:{'LoadState':'not-found'})
        self.assertEqual('unknown',report['configured_mode'])
        self.assertEqual('unknown',report['timer_state'])
        self.assertFalse(report['command_matches'])

    def test_explicit_live_and_active_timer(self):
        def probe(unit):
            if unit.endswith('.timer'):return dict(LoadState='loaded',ActiveState='active',UnitFileState='enabled')
            return dict(LoadState='loaded',ActiveState='active',ExecStart='/app/nifty_30m_reference_live.py --preset pe1-v1 --live')
        report=observe('nifty_30m_reference',probe)
        self.assertEqual('live',report['configured_mode'])
        self.assertTrue(report['command_matches'])
        self.assertEqual('active',report['timer_state'])

    def test_v3_command_cannot_claim_to_be_pe1(self):
        report=observe('nifty_30m_reference',lambda unit:dict(ExecStart='/app/nifty_30m_reference_live.py --live'))
        self.assertFalse(report['command_matches'])
        self.assertEqual('unknown',report['configured_mode'])

    def test_default_paper_command(self):
        report=observe('fixed_profit_filtered',lambda unit:dict(ExecStart='/app/fixed_profit_filtered_live.py --user-id 1'))
        self.assertEqual('paper',report['configured_mode'])

    def test_another_accounts_service_cannot_claim_this_accounts_mode(self):
        report=observe('fixed_profit_filtered',lambda unit:dict(
            ExecStart='/app/fixed_profit_filtered_live.py --user-id 2 --live'),expected_user_id=1)
        self.assertFalse(report['command_matches'])
        self.assertEqual('unknown',report['configured_mode'])
