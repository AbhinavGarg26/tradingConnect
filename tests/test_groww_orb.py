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


def test_breakout_rejects_a_close_at_or_after_1100():
    rows = session()
    for i, row in enumerate(rows):
        if i >= 60:
            row.update(open=20000, high=20001, low=19999, close=20000)
    # The 10:59 candle completes at 11:00; it must not qualify.
    rows[104].update(open=20120, high=20121, low=20119, close=20120)
    assert run(rows, 'breakout') == []


def test_breakout_rejects_a_pe_signal():
    rows = session()
    rows[60].update(open=19880, high=19881, low=19879, close=19880)
    assert run(rows, 'breakout') == []


def test_reversal_rejects_a_ce_signal():
    rows = session()
    for i, row in enumerate(rows):
        row.update(open=20000, high=20001, low=19999, close=20000)
        if i == 0:
            row.update(high=20110, low=19990)
    rows[5].update(open=19880, high=19881, low=19879, close=19880)
    rows[7].update(open=20120, high=20121, low=20119, close=20120)
    assert run(rows, 'reversal') == []


def test_no_retest_without_return_inside_range():
    assert run(session(), 'retest') == []
