from datetime import datetime, timedelta
from decimal import Decimal as D

import pytest

from strategies.nifty_15m_breakout import Bar
from strategies.nifty_opening_range import IST
from strategies.small_profit_price_action import SmallProfitPriceAction

START = datetime(2026, 11, 9, 9, 15, tzinfo=IST)


def bar(minute, o, h, l, c):
    return Bar(START + timedelta(minutes=minute), *(D(str(x)) for x in (o, h, l, c)))


def engine(**kwargs):
    strategy = SmallProfitPriceAction(min_range_points=20, **kwargs)
    strategy.set_reference(bar(0, 110, 130, 100, 120), observed_at=START + timedelta(minutes=30))
    return strategy


def feed(strategy, candle):
    return strategy.on_bar(candle, observed_at=candle.time + timedelta(minutes=5))


def enter_and_close(strategy, pnl):
    feed(strategy, bar(30, 129, 136, 128, 134))
    feed(strategy, bar(35, 132, 134, 129, 133))
    signal = feed(strategy, bar(40, 133, 137, 132, 136))
    assert signal.action == "BUY_CE"
    strategy.confirm_entry(index_price=136)
    strategy.pending = type(signal)("EXIT_CE", signal.time, signal.index_price, "target")
    strategy.confirm_exit()
    return strategy.record_closed_trade(pnl)


def test_price_action_entry_uses_small_fixed_index_risk():
    strategy = engine()
    enter_and_close(strategy, 1000)
    assert strategy.stop_points == 8
    assert strategy.target_points == 12
    assert strategy.session_pnl == 1000


def test_daily_profit_is_a_lock_not_a_promise():
    strategy = engine(daily_profit_target=5000)
    assert enter_and_close(strategy, 5100) == "daily_profit_target"
    assert not strategy.can_enter
    assert feed(strategy, bar(45, 129, 136, 128, 134)) is None


def test_loss_and_consecutive_loss_circuit_breakers():
    strategy = engine(max_daily_loss=2000)
    assert enter_and_close(strategy, -2100) == "max_daily_loss"
    strategy = engine(max_daily_loss=5000, max_consecutive_losses=1)
    assert enter_and_close(strategy, -500) == "consecutive_losses"


@pytest.mark.parametrize("field,value", [("daily_profit_target", 0), ("max_daily_loss", -1)])
def test_invalid_money_limits_fail_closed(field, value):
    with pytest.raises(ValueError):
        SmallProfitPriceAction(**{field: value})
