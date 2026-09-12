from datetime import date

from instrument_catalog import catalog_values


def test_catalog_values_maps_kite_master_record():
    values = catalog_values({
        "tradingsymbol": "nifty26sep25000ce", "exchange": "NFO",
        "segment": "NFO-OPT", "instrument_type": "CE",
        "instrument_token": 12345, "lot_size": 75, "tick_size": 0.05,
        "expiry": "2026-09-24", "strike": 25000,
    })

    assert values["symbol"] == "NIFTY26SEP25000CE"
    assert values["expiry_date"] == date(2026, 9, 24)
    assert values["lot_size"] == 75


def test_zero_strike_and_empty_expiry_become_null():
    values = catalog_values({
        "tradingsymbol": "INFY", "exchange": "NSE",
        "instrument_token": 99, "instrument_type": "EQ",
        "expiry": "", "strike": 0,
    })

    assert values["strike_price"] is None
    assert values["expiry_date"] is None
