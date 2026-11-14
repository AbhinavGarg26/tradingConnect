from datetime import date
from decimal import Decimal as D

import pytest

from nifty_15m_env import load_nifty_environment
load_nifty_environment()

from one_direction_profit_lock_service import nearest_weekly_atm
from trading.reference_live_execution import confirm_market_order


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


def test_live_order_accepts_strategy_specific_broker_tag():
    class Kite:
        VARIETY_REGULAR = "regular"; PRODUCT_MIS = "MIS"; ORDER_TYPE_MARKET = "MARKET"
        VALIDITY_DAY = "DAY"; TRANSACTION_TYPE_BUY = "BUY"
        def __init__(self): self.sent = None
        def order_history(self, _):
            return [{"status": "COMPLETE", "filled_quantity": 25, "average_price": 100}]
    kite = Kite()
    from unittest.mock import Mock, patch
    contract = {"tradingsymbol": "NIFTYTEST"}
    with patch("trading.reference_live_execution.place_protected_market_order",
               side_effect=lambda _kite, **kwargs: setattr(kite, "sent", kwargs) or "1"):
        confirm_market_order(kite, contract, "BUY", 25, Mock(), tag="ODPROFITV21")
    assert kite.sent["tag"] == "ODPROFITV21"
