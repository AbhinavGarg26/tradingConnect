from datetime import datetime, timedelta
from decimal import Decimal as D
import pytest

from strategies.fixed_profit_trend_gate import TrendSnapshot, allows_entry
from strategies.nifty_opening_range import IST
from strategies.nifty_15m_breakout import Bar
from backtest.fixed_profit_trend_gate import schedule

NOW=datetime(2026,9,21,12,45,tzinfo=IST)


def snapshot(close=110,ema36=100,ema100=105):
    return TrendSnapshot(NOW,D(close),D(ema36),D(ema100))


def test_both_price_and_closed_candle_must_align():
    assert allows_entry(111,'CE',snapshot(),observed_at=NOW)
    assert not allows_entry(103,'CE',snapshot(),observed_at=NOW)
    assert not allows_entry(111,'CE',snapshot(close=104),observed_at=NOW)
    assert allows_entry(111,'CE',snapshot(close=104),observed_at=NOW,require_close=False)
    assert allows_entry(90,'PE',snapshot(close=95),observed_at=NOW)


def test_only36_can_pass_while_dual_ema_rejects():
    values=snapshot(close=103)
    assert allows_entry(104,'CE',values,observed_at=NOW,periods=(36,))
    assert not allows_entry(104,'CE',values,observed_at=NOW)


def test_future_snapshot_and_equal_price_are_rejected():
    with pytest.raises(ValueError,match='before entry'):
        allows_entry(111,'CE',snapshot(),observed_at=NOW-timedelta(seconds=1))
    assert not allows_entry(105,'CE',snapshot(),observed_at=NOW)


def test_schedule_has_no_intrabar_or_future_price_leakage():
    start=datetime(2020,1,1,9,15,tzinfo=IST)
    sessions=[]
    for day in range(26):
        bars=[Bar(start+timedelta(days=day,minutes=i),D(100),D(101),D(99),D(100)) for i in range(375)]
        sessions.append((bars[0].time.date(),bars))
    original,_=schedule(sessions)
    assert any(t.time().hour==15 and t.time().minute==30 for t in original)
    day,bars=sessions[-1]
    changed_bars=[Bar(b.time,D(200),D(201),D(199),D(200)) for b in bars]
    changed,_=schedule(sessions[:-1]+[(day,changed_bars)])
    assert all(changed[t]==value for t,value in original.items() if t.date()<day)
    assert all(t.time().minute in {15,30,45} for t in original)
    assert all(value.close==100 for value in original.values())
