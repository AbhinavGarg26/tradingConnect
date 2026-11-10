from datetime import datetime, time, timedelta
from types import SimpleNamespace

from strategies.nifty_opening_range import IST

import pytest

from backtest.nifty_noise_area import Config, check_indexes


def test_invalid_noise_area_configuration_fails_closed():
    with pytest.raises(ValueError):
        Config(lookback=1)
    with pytest.raises(ValueError):
        Config(cost_points=-1)
    with pytest.raises(ValueError):
        Config(max_entries=0)


def test_checks_are_completed_half_hour_observations():
    start = datetime(2026, 11, 10, 9, 15, tzinfo=IST)
    bars = [SimpleNamespace(time=start + timedelta(minutes=i)) for i in range(375)]
    indexes = check_indexes(bars, Config())
    assert bars[indexes[0]].time.time() == time(9, 44)
    assert bars[indexes[1]].time.time() == time(10, 14)
