from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pytest
from strategies.groww_orb_intraday import GrowwOrbIntraday


def test_breakout_only_emits_once_and_uses_completed_close():
    engine = GrowwOrbIntraday()
    start = datetime(2026, 11, 20, 9, 15, tzinfo=ZoneInfo('Asia/Kolkata'))
    signals = []
    for i in range(63):
        price = 20120 if i >= 60 else 20000
        signals += engine.on_bar(dict(date=start + timedelta(minutes=i), open=price,
            high=20110 if i == 0 else price + 1, low=19990 if i == 0 else price - 1, close=price))
    assert [(s['setup'], s['side']) for s in signals] == [('breakout', 'CE')]
    assert signals[0]['signal_time'].hour == 10 and signals[0]['signal_time'].minute == 16


def test_missing_candle_fails_closed():
    engine = GrowwOrbIntraday()
    start = datetime(2026, 11, 20, 9, 15, tzinfo=ZoneInfo('Asia/Kolkata'))
    row = dict(date=start, open=100, high=101, low=99, close=100)
    engine.on_bar(row)
    with pytest.raises(ValueError, match='consecutive'):
        engine.on_bar(dict(row, date=start + timedelta(minutes=2)))
