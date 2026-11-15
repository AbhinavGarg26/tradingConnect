from strategies.smallcap_momentum_swing import Action, Candle, confirm_strong_start


def candle(open_, high, low, close, volume=100):
    return Candle(open_, high, low, close, volume)


def test_strong_start_requires_upper_30pct_close_above_setup_high():
    previous = candle(95, 100, 94, 99)
    weak_close = [candle(101, 105, 98, 100.5)]

    decision = confirm_strong_start(previous, weak_close, same_time_volume_ratio=2.0)

    assert decision.action == Action.NONE
    assert decision.reason == "opening_close_not_in_upper_30pct"


def test_strong_start_accepts_15m_hold_high_close_and_relative_volume():
    previous = candle(95, 100, 94, 99)
    strong_close = [candle(101, 105, 98, 103.5)]

    decision = confirm_strong_start(previous, strong_close, same_time_volume_ratio=1.5)

    assert decision.action == Action.ENTER
    assert decision.entry_trigger == 101
    assert decision.initial_stop == 98


def test_strong_start_rejects_close_below_setup_high_even_if_range_location_is_strong():
    previous = candle(95, 100, 94, 99)
    failed_break = [candle(101, 102, 98, 99.5)]

    decision = confirm_strong_start(previous, failed_break, same_time_volume_ratio=2.0)

    assert decision.action == Action.NONE
    assert decision.reason == "opening_close_failed_previous_high"
