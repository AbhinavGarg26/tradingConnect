from datetime import date

from backtest.gap_up_volume_buildup_research import prior_volume_signal
from strategies.smallcap_momentum_swing import Candle


def rows(volumes, closes=None):
    closes = closes or [100 + index for index in range(len(volumes))]
    return [(date(2026, 1, index + 1),
             Candle(close - 1, close + 1, close - 2, close, volume))
            for index, (volume, close) in enumerate(zip(volumes, closes))]


def test_prior_volume_signal_uses_only_completed_sessions_before_gap():
    values = rows([100] * 20 + [200, 100, 100, 100, 10_000])
    signal = prior_volume_signal(values, 24, 4, 1.5, "any")
    assert signal is not None
    assert signal[1] == "2026-01-21"
    # The gap day's huge volume at index 24 must never affect the signal.
    assert signal[0] == 2.0


def test_strong_mode_requires_bullish_upper_range_accumulation():
    closes = [100] * 20 + [99, 100, 100, 100, 100]
    values = rows([100] * 20 + [250, 100, 100, 100, 100], closes)
    assert prior_volume_signal(values, 24, 4, 2.0, "any") is not None
    assert prior_volume_signal(values, 24, 4, 2.0, "strong") is None
