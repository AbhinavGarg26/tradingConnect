from datetime import date
from decimal import Decimal as D

import pytest

from one_direction_profit_lock_service import nearest_weekly_atm


def test_selects_nearest_unexpired_expiry_and_atm_strike():
    instruments = [
        {"name": "NIFTY", "instrument_type": side, "expiry": expiry,
         "strike": strike, "tradingsymbol": f"NIFTY-{expiry}-{strike}-{side}"}
        for expiry in (date(2026, 11, 17), date(2026, 11, 24))
        for strike in (25200, 25250, 25300)
        for side in ("CE", "PE")
    ]
    contract = nearest_weekly_atm(instruments, date(2026, 11, 14), "PE", D("25260"))
    assert contract["expiry"] == date(2026, 11, 17)
    assert contract["strike"] == 25250
    assert contract["instrument_type"] == "PE"


def test_missing_current_expiry_fails_closed():
    with pytest.raises(ValueError, match="No current"):
        nearest_weekly_atm([], date(2026, 11, 14), "CE", D("25250"))
