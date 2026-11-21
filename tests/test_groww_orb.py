from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from backtest.groww_orb import run


def session(opening=20000):
    start = datetime(2026, 11, 20, 9, 15, tzinfo=ZoneInfo('Asia/Kolkata'))
    rows = []
    for i in range(375):
        close = opening
        if i == 60:
            close = opening + 120
        if i >= 61:
            close = opening + 150
        rows.append(dict(date=(start + timedelta(minutes=i)).isoformat(), open=close,
                         high=close + (110 if i == 0 else 1),
                         low=close - (10 if i == 0 else 1), close=close))
    return rows


def test_breakout_requires_wide_hour_and_fills_after_signal():
    rows = session()
    trades = run(rows, 'breakout', slippage=1)
    assert len(trades) == 1
    assert trades[0]['entry_time'].endswith('10:16:00+05:30')
    assert trades[0]['entry'] == 20151
    assert trades[0]['reason'] == 'session_end'


def test_missing_minute_rejected_instead_of_lookahead():
    rows = session()
    rows.pop(100)
    with pytest.raises(ValueError, match='expected every minute'):
        run(rows, 'breakout')


def test_no_retest_without_return_inside_range():
    assert run(session(), 'retest') == []
