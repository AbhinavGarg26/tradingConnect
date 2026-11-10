from datetime import datetime, time
from decimal import Decimal

from strategies.nifty_opening_range import IST
from strategies.nifty_noise_area import NoiseAreaMomentum


def engine():
    clocks = [time(hour, minute) for hour, minute in
              ((9,45),(10,15),(10,45),(11,15),(11,45),(12,15),(12,45),
               (13,15),(13,45),(14,15),(14,45))]
    return NoiseAreaMomentum({clock: [Decimal("0.01")] * 14 for clock in clocks}, 100)


def test_completed_half_hour_break_and_inside_exit():
    value = engine(); start = datetime(2026, 11, 10, 9, 15, tzinfo=IST)
    value.start_session(start, 100)
    assert value.on_minute(start.replace(hour=9, minute=44), Decimal("102")).action == "BUY_CE"
    value.confirm_entry()
    assert value.on_minute(start.replace(hour=10, minute=14), Decimal("100.5")).reason == "inside_noise_area"
    value.confirm_exit(); assert value.position is None


def test_session_exit_is_mandatory():
    value = engine(); start = datetime(2026, 11, 10, 9, 15, tzinfo=IST)
    value.start_session(start, 100); value.position = "PE"
    signal = value.on_minute(start.replace(hour=15, minute=14), Decimal("99"))
    assert signal.action == "EXIT_PE" and signal.reason == "session_end"
