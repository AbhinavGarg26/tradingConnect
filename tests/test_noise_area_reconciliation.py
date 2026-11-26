from decimal import Decimal
from unittest.mock import Mock

from nifty_15m_env import load_nifty_environment
load_nifty_environment()

from trading.noise_area_reconciliation import confirmed_fill


def test_full_confirmed_broker_fill_is_usable():
    kite = Mock()
    kite.order_history.return_value = [{"order_id": "123", "status": "COMPLETE",
                                        "filled_quantity": 65, "average_price": 42.55}]
    assert confirmed_fill(kite, "123", 65)[0] == Decimal("42.55")


def test_partial_or_unverified_orders_do_not_supply_a_price():
    kite = Mock()
    for order in ({"status": "OPEN", "filled_quantity": 65, "average_price": 42},
                  {"status": "COMPLETE", "filled_quantity": 10, "average_price": 42},
                  {"status": "COMPLETE", "filled_quantity": 65, "average_price": 0},
                  {"order_id": "other", "status": "COMPLETE", "filled_quantity": 65,
                   "average_price": 42}):
        kite.order_history.return_value = [order]
        assert confirmed_fill(kite, "123", 65) is None
