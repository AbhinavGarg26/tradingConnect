from datetime import datetime, time
from decimal import Decimal as D

import pytest

from strategies.nifty_opening_range import IST
from strategies.one_direction_profit_lock import OneDirectionProfitLock, TrendSnapshot


NOW = datetime(2026, 11, 13, 11, 0, tzinfo=IST)


def trend(direction="CE", observed_at=NOW):
    if direction == "CE":
        return TrendSnapshot(observed_at, D(101), D(100), D(99), D(100), D(28), D(30), D(18))
    return TrendSnapshot(observed_at, D(99), D(100), D(101), D(100), D(28), D(18), D(30))


def test_market_flow_must_agree_and_first_entry_locks_direction():
    gate = OneDirectionProfitLock()
    assert gate.allow_entry("CE", trend("CE")) == (True, "allowed")
    assert gate.allow_entry("PE", trend("CE")) == (False, "against_market_flow")
    gate.record_entry("CE", trend("CE"))
    assert gate.allow_entry("PE", trend("PE")) == (False, "position_open")
    gate.record_closed_trade(100)
    assert gate.allow_entry("PE", trend("PE")) == (False, "session_direction_locked")


def test_profit_target_and_loss_streak_stop_new_entries():
    gate = OneDirectionProfitLock()
    gate.record_entry("CE", trend())
    assert gate.record_closed_trade(2100) == "daily_profit_locked"
    assert gate.allow_entry("CE", trend()) == (False, "daily_profit_locked")

    gate = OneDirectionProfitLock()
    gate.record_entry("CE", trend())
    assert gate.record_closed_trade(-100) is None
    gate.record_entry("CE", trend())
    assert gate.record_closed_trade(-100) == "consecutive_losses"


def test_entry_count_and_cutoff_fail_closed():
    gate = OneDirectionProfitLock(max_entries=1)
    gate.record_entry("CE", trend())
    assert gate.record_closed_trade(100) == "max_entries"
    late = trend(observed_at=NOW.replace(hour=12, minute=0))
    assert OneDirectionProfitLock().allow_entry("CE", late) == (False, "entry_cutoff")


def test_profitable_position_stop_locks_and_never_moves_backward():
    gate = OneDirectionProfitLock()
    assert gate.protected_stop(100, 109) is None
    assert gate.protected_stop(100, 110) == D("105.00")
    assert gate.protected_stop(100, 120) == D("110.00")
    assert gate.protected_stop(100, 120, current_stop=112) == D("112")


def test_initial_stop_and_position_size_cap_planned_loss():
    gate = OneDirectionProfitLock(max_loss_per_trade=1500)
    stop = gate.initial_stop(100, 10)
    assert stop == D("90.00")
    assert gate.risk_sized_quantity(100, stop, 25) == 150
    with pytest.raises(ValueError, match="one lot"):
        gate.risk_sized_quantity(1000, 900, 25)


def test_session_peak_floor_locks_seventy_percent():
    gate = OneDirectionProfitLock()
    assert gate.update_session_pnl(5000) is None
    assert gate.session_profit_floor == D("3500")
    assert gate.update_session_pnl(10000) is None
    assert gate.session_profit_floor == D("7000")
    assert gate.update_session_pnl(6999) == "session_profit_floor"


def test_reversal_needs_two_candles_cooldown_and_only_one_acceptance():
    gate = OneDirectionProfitLock()
    gate.record_entry("CE", trend("CE"))
    gate.record_exit(NOW)
    early = trend("PE", observed_at=NOW.replace(minute=10))
    assert gate.observe_reversal(early) is None
    first = trend("PE", observed_at=NOW.replace(minute=15))
    second = trend("PE", observed_at=NOW.replace(minute=20))
    assert gate.observe_reversal(first) is None
    assert gate.observe_reversal(second) == "PE"
    gate.accept_reversal("PE", second.observed_at)
    assert gate.direction == "PE"
    assert gate.reversals == 1
    assert gate.observe_reversal(trend("CE", observed_at=NOW.replace(minute=30))) is None


def test_confirmed_reversal_is_accepted_before_recording_its_exit_time():
    gate = OneDirectionProfitLock()
    gate.record_entry("CE", trend("CE"))
    first = trend("PE", observed_at=NOW.replace(minute=15))
    second = trend("PE", observed_at=NOW.replace(minute=30))
    assert gate.observe_reversal(first) is None
    assert gate.observe_reversal(second) == "PE"

    gate.record_closed_trade(100)
    gate.accept_reversal("PE", second.observed_at)
    gate.record_exit(second.observed_at)

    assert gate.direction == "PE"
    assert gate.last_exit_at == second.observed_at


def test_state_round_trip_and_invalid_state_fail_closed():
    gate = OneDirectionProfitLock()
    gate.record_entry("CE", trend())
    gate.update_session_pnl(6000)
    restored = OneDirectionProfitLock()
    restored.restore(gate.state())
    assert restored.state() == gate.state()
    with pytest.raises(ValueError, match="invalid persisted"):
        restored.restore({})
    assert restored.lock_reason == "invalid_persisted_state"


@pytest.mark.parametrize("field", ["daily_profit_lock", "max_daily_loss", "min_adx"])
def test_invalid_limits_are_rejected(field):
    with pytest.raises(ValueError):
        OneDirectionProfitLock(**{field: 0})
