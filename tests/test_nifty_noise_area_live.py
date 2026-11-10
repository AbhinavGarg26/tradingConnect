from datetime import datetime
from unittest.mock import Mock

import pytest

try:
    from sqlalchemy.orm import DeclarativeBase  # noqa: F401
except ImportError:
    pytest.skip("Live runner tests require the production SQLAlchemy 2 runtime", allow_module_level=True)

from nifty_noise_area_live import confirmed_order, startup_allowed
from strategies.nifty_opening_range import IST
from trading.reference_live_execution import UncertainOrder


def test_live_start_requires_early_session_window():
    assert startup_allowed(datetime(2026, 11, 10, 9, 20, tzinfo=IST))
    assert not startup_allowed(datetime(2026, 11, 10, 9, 40, tzinfo=IST))


def test_confirmed_order_submits_once_and_requires_full_fill():
    kite = Mock(); kite.VARIETY_REGULAR = "regular"; kite.PRODUCT_MIS = "MIS"
    kite.ORDER_TYPE_MARKET = "MARKET"; kite.VALIDITY_DAY = "DAY"
    kite.place_order.return_value = "order-1"
    kite.order_history.return_value = [{"status": "COMPLETE", "average_price": 100,
                                        "filled_quantity": 65}]
    journal = Mock(); contract = {"tradingsymbol": "NIFTYCE"}
    order, price = confirmed_order(kite, contract, "BUY", 65, journal)
    assert order == "order-1" and str(price) == "100"
    kite.place_order.assert_called_once()


def test_rejected_order_is_never_retried():
    kite = Mock(); kite.VARIETY_REGULAR = "regular"; kite.PRODUCT_MIS = "MIS"
    kite.ORDER_TYPE_MARKET = "MARKET"; kite.VALIDITY_DAY = "DAY"
    kite.place_order.return_value = "order-1"
    kite.order_history.return_value = [{"status": "REJECTED"}]
    with pytest.raises(UncertainOrder):
        confirmed_order(kite, {"tradingsymbol": "NIFTYPE"}, "BUY", 65, Mock())
    kite.place_order.assert_called_once()
