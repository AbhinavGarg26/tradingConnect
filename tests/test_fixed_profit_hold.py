from datetime import datetime, timedelta
from decimal import Decimal
import pytest
from strategies.fixed_profit_hold import FixedProfit
from strategies.nifty_15m_breakout import Bar
from strategies.nifty_opening_range import IST


def setup_engine(threshold=20):
    engine = FixedProfit(min_range_points=threshold, validation_minutes=2)
    start = datetime(2026, 10, 23, 9, 15, tzinfo=IST)
    engine.set_reference(Bar(start, Decimal(100), Decimal(130), Decimal(100), Decimal(120)),
                         observed_at=start + timedelta(minutes=30))
    return engine, start + timedelta(minutes=30)


@pytest.mark.parametrize('side,price,target,stop', [('CE', 131, 161, 121), ('PE', 99, 69, 109)])
def test_hold_and_fixed_exits(side, price, target, stop):
    engine, now = setup_engine()
    assert engine.on_tick(now, price) is None
    assert engine.on_tick(now + timedelta(minutes=1), price) is None
    signal = engine.on_tick(now + timedelta(minutes=2), price)
    assert signal.action == 'BUY_' + side
    assert engine.on_tick(now + timedelta(minutes=3), price) is None
    engine.confirm_entry(index_price=price, option_fill=50)
    assert (engine.target, engine.stop) == (target, stop)
    assert engine.on_tick(now + timedelta(minutes=4), target).reason == 'target'
    engine.confirm_exit()
    assert engine.on_tick(now + timedelta(minutes=5), price) is None


def test_return_inside_restarts_hold():
    engine, now = setup_engine()
    engine.on_tick(now, 131)
    engine.on_tick(now + timedelta(minutes=1), 130)
    assert engine.on_tick(now + timedelta(minutes=2), 131) is None
    assert engine.on_tick(now + timedelta(minutes=4), 131).action == 'BUY_CE'


def test_threshold_strictly_exceeded():
    engine, now = setup_engine(30)
    assert engine.on_tick(now, 131) is None
    assert engine.on_tick(now + timedelta(minutes=10), 131) is None


def test_rejection_restarts_validation():
    engine, now = setup_engine()
    engine.on_tick(now, 131)
    engine.on_tick(now + timedelta(minutes=2), 131)
    engine.reject_signal()
    assert engine.on_tick(now + timedelta(minutes=3), 131) is None


def test_stop_and_session_checks():
    engine, now = setup_engine()
    engine.on_tick(now, 131)
    engine.on_tick(now + timedelta(minutes=2), 131)
    engine.confirm_entry(index_price=131, option_fill=50)
    assert engine.on_tick(now + timedelta(minutes=3), 120).reason == 'stop'
    with pytest.raises(ValueError):
        engine.on_tick(now - timedelta(minutes=1), 120)
