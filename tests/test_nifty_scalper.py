from datetime import datetime, timedelta
from decimal import Decimal as D

import pytest

from strategies.nifty_15m_breakout import Bar
from strategies.nifty_opening_range import IST
from strategies.nifty_scalper import NiftyScalper
from backtest.nifty_scalper_research import promotion_check

START = datetime(2026, 11, 9, 9, 15, tzinfo=IST)


def bar(index, o, h, l, c):
    return Bar(START + timedelta(minutes=5 * index), *(D(str(x)) for x in (o, h, l, c)))


def feed(engine, candle):
    return engine.on_bar(candle, observed_at=candle.time + timedelta(minutes=5))


def warm_bullish(engine):
    price = D("100")
    for i in range(13):
        feed(engine, bar(i, price, price + 2, price - 1, price + 1))
        price += 1


def test_completed_bar_is_required():
    engine = NiftyScalper()
    candle = bar(0, 100, 102, 99, 101)
    with pytest.raises(ValueError, match="completed"):
        engine.on_bar(candle, observed_at=candle.time + timedelta(minutes=4))


def test_bullish_pullback_breakout_emits_ce_and_builds_bounded_risk():
    engine = NiftyScalper(entry_start=datetime.min.time())
    warm_bullish(engine)
    # Pullback touches the fast EMA, then continuation closes above its high.
    prior_fast = engine.fast
    feed(engine, bar(13, prior_fast + 1, prior_fast + 2, prior_fast - 1, prior_fast + 1))
    signal = feed(engine, bar(14, prior_fast + 1, prior_fast + 5, prior_fast, prior_fast + 4))
    assert signal.action == "BUY_CE"
    assert D("8") <= signal.stop_distance <= D("18")
    engine.confirm_entry(index_price=signal.index_price)
    assert engine.stop < engine.entry < engine.target


def test_one_r_moves_stop_to_break_even_and_target_closes():
    engine = NiftyScalper(entry_start=datetime.min.time())
    warm_bullish(engine)
    prior_fast = engine.fast
    feed(engine, bar(13, prior_fast + 1, prior_fast + 2, prior_fast - 1, prior_fast + 1))
    signal = feed(engine, bar(14, prior_fast + 1, prior_fast + 5, prior_fast, prior_fast + 4))
    engine.confirm_entry(index_price=signal.index_price)
    risk = engine.entry - engine.stop
    engine.on_tick(START + timedelta(hours=2), engine.entry + risk)
    assert engine.stop == engine.entry
    exit_signal = engine.on_tick(START + timedelta(hours=2, minutes=1), engine.target)
    assert exit_signal.reason == "target"
    assert engine.confirm_exit(index_price=engine.target, timestamp=exit_signal.time) > 0


def test_two_losses_lock_the_session():
    engine = NiftyScalper(max_losses=2)
    for number in range(2):
        engine.position, engine.entry, engine.stop, engine.target = "CE", D("100"), D("90"), D("115")
        engine.pending = type("Signal", (), {"action": "EXIT_CE"})()
        engine.confirm_exit(index_price=90, timestamp=START + timedelta(minutes=number * 20))
    assert engine.lock_reason == "consecutive_losses"
    assert not engine.can_enter


@pytest.mark.parametrize("field,value", [("fast_period", 1), ("atr_multiplier", 0),
                                          ("min_stop", 20), ("daily_loss_points", -1)])
def test_invalid_configuration_fails_closed(field, value):
    kwargs = {field: value}
    if field == "min_stop":
        kwargs["max_stop"] = 18
    with pytest.raises(ValueError):
        NiftyScalper(**kwargs)


def test_research_promotion_fails_when_validation_is_negative():
    healthy = {"trades": 300, "net_points": 100, "profit_factor": 1.2,
               "max_drawdown_points": 200}
    score = {
        "development": healthy,
        "validation": {**healthy, "trades": 150, "net_points": -1},
        "holdout": {**healthy, "trades": 75},
        "all_cost_2": healthy,
    }
    decision = promotion_check(score)
    assert not decision["accepted"]
    assert not decision["requirements"]["validation_positive"]


def test_research_promotion_accepts_only_complete_healthy_periods():
    score = {
        "development": {"trades": 300, "net_points": 100, "profit_factor": 1.2},
        "validation": {"trades": 150, "net_points": 50, "profit_factor": 1.1},
        "holdout": {"trades": 75, "net_points": 25, "profit_factor": 1.08},
        "all_cost_2": {"max_drawdown_points": 400},
    }
    assert promotion_check(score)["accepted"]
