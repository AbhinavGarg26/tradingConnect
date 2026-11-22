from datetime import datetime

from market.overnight_protection import carried_entry_price, overnight_protection_active, IST


def test_carried_entry_requires_entire_position_to_be_overnight():
    assert carried_entry_price({"quantity": 50, "overnight_quantity": 50,
                                "overnight_average_price": 100}) == 100
    assert carried_entry_price({"quantity": 100, "overnight_quantity": 50,
                                "overnight_average_price": 100}) is None


def test_overnight_stops_begin_at_0930_ist():
    assert not overnight_protection_active(datetime(2026, 11, 20, 9, 29, tzinfo=IST))
    assert overnight_protection_active(datetime(2026, 11, 20, 9, 30, tzinfo=IST))
