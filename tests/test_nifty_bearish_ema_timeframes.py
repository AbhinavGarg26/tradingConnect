import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backtest.nifty_bearish_ema_timeframes import verify_baseline


class FrozenBearishBaselineTests(unittest.TestCase):
    def test_frozen_inputs_verified_and_changes_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'candles.csv').write_text('source candles\n')
            (root/'trades.csv').write_text('original trades\n')
            manifest=dict(source_candles='candles.csv',historical_trade_file='trades.csv',
                          source_candles_sha256=hashlib.sha256((root/'candles.csv').read_bytes()).hexdigest(),
                          historical_trade_sha256=hashlib.sha256((root/'trades.csv').read_bytes()).hexdigest())
            baseline=root/'baseline.json'
            baseline.write_text(json.dumps(manifest))
            with patch('backtest.nifty_bearish_ema_timeframes.ROOT',root),patch('backtest.nifty_bearish_ema_timeframes.BASELINE',baseline):
                self.assertEqual(manifest,verify_baseline())
                (root/'trades.csv').write_text('different trades\n')
                with self.assertRaisesRegex(ValueError,'Frozen baseline'):
                    verify_baseline()

    def test_actual_selected_baseline_remains_15m_9_21(self):
        baseline=verify_baseline()
        self.assertEqual((9,21,15),tuple(baseline['rules'][k] for k in ('fast','slow','timeframe_minutes')))
        self.assertEqual(680,baseline['frozen_metrics']['closed_trades'])
        self.assertIn('not activated',baseline['status'])


if __name__=='__main__':
    unittest.main()
