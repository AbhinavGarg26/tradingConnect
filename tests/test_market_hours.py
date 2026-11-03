from datetime import datetime, timezone

from market.market import is_market_open


def test_market_is_open_on_utc_host_during_indian_session():
    assert is_market_open(datetime(2026, 10, 28, 3, 45, tzinfo=timezone.utc))


def test_market_is_closed_before_indian_session_on_utc_host():
    assert not is_market_open(datetime(2026, 10, 28, 3, 15, tzinfo=timezone.utc))


def test_naive_time_is_treated_as_indian_local_time_for_compatibility():
    assert is_market_open(datetime(2026, 10, 28, 9, 15))
