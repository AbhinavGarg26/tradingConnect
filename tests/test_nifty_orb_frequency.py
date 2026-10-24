import unittest
from dataclasses import asdict
from datetime import datetime, timedelta

from backtest.nifty_orb_frequency import experiments, frozen_config, research
from backtest.nifty_orb_pullback import Config, run
from tests.test_nifty_orb_pullback import setup_rows


class FrequencyTests(unittest.TestCase):
    def test_frozen_baseline_matches_original_selective_defaults(self):
        self.assertEqual(asdict(Config(selective=True)), asdict(frozen_config()))
        self.assertEqual(run(setup_rows(), Config(selective=True)), run(setup_rows(), frozen_config()))

    def test_predeclared_changes_keep_risk_and_target(self):
        base = frozen_config()
        allowed = {
            'selective_v1': set(), 'opening_40pct': {'opening_direction_ratio'},
            'breakout_body_40pct': {'breakout_body_ratio'}, 'retest_030atr': {'retest_band_atr'},
            'pullback_12bars': {'pullback_bars'}, 'wait_strong_breakout': {'wait_for_strong_breakout'},
            'opening40_retest030': {'opening_direction_ratio', 'retest_band_atr'},
        }
        variants = experiments(base)
        self.assertEqual(set(allowed), set(variants))
        for name, config in variants.items():
            changes = {k for k, v in asdict(config).items() if v != asdict(base)[k]}
            self.assertEqual(allowed[name], changes)
            self.assertEqual(2, config.reward)
            self.assertEqual(.5, config.slippage)
            self.assertTrue(config.selective)

    def test_report_partitions_and_entry_accounting(self):
        source = setup_rows()
        rows = []
        for i in range(900):
            row = dict(source[i % len(source)])
            row['date'] = (datetime(2026, 1, 1, 9, 15) + timedelta(days=i//75, minutes=5*(i%75))).isoformat()
            rows.append(row)
        report, trade_sets = research(rows)
        self.assertEqual(12, report['sessions'])
        baseline_count = len(trade_sets['selective_v1'])
        for name, result in report['variants'].items():
            self.assertEqual(result['all']['trades'], result['development']['trades']+result['validation']['trades'])
            self.assertEqual(baseline_count, result['baseline_entries_retained']+result['baseline_entries_missing'])
            self.assertEqual(len(trade_sets[name]), result['baseline_entries_retained']+result['added_trades']['trades'])
            self.assertEqual(result['all']['trades'], sum(m['trades'] for m in result['monthly'].values()))
            self.assertEqual(result['all']['trades'], sum(y['trades'] for y in result['yearly'].values()))

    def test_invalid_experiment_inputs(self):
        for kw in ({'opening_direction_ratio': 1.1}, {'breakout_body_ratio': -1},
                   {'pullback_bars': 0}, {'pullback_bars': 6.5}, {'retest_band_atr': -1}):
            with self.assertRaises(ValueError):
                Config(**kw)


if __name__ == '__main__':
    unittest.main()
