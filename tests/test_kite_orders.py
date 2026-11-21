import unittest

from kiteconnect import KiteConnect
from kiteconnect.exceptions import InputException

from market.kite_orders import (
    place_market_order_with_depth_fallback,
    place_protected_market_order,
)


class OldSdkKite:
    def place_order(self, variety, exchange, tradingsymbol, transaction_type,
                    quantity, product, order_type, validity=None, tag=None):
        raise AssertionError("old public method must not be used")

    def __init__(self):
        self.posted = None

    def _post(self, route, url_args, params):
        self.posted = (route, url_args, params)
        return {"order_id": "OLD-SDK-ORDER"}


class KiteOrderCompatibilityTests(unittest.TestCase):
    def test_old_sdk_posts_automatic_market_protection(self):
        kite = OldSdkKite()
        order_id = place_protected_market_order(
            kite,
            variety="regular",
            exchange="NFO",
            tradingsymbol="NIFTYTESTCE",
            transaction_type="SELL",
            quantity=65,
            product="MIS",
            order_type="MARKET",
            validity="DAY",
        )

        self.assertEqual(order_id, "OLD-SDK-ORDER")
        route, url_args, params = kite.posted
        self.assertEqual(route, "order.place")
        self.assertEqual(url_args, {"variety": "regular"})
        self.assertEqual(params["market_protection"], -1)
        self.assertNotIn("variety", params)

    def test_illiquid_stock_option_market_rejection_uses_best_bid_for_sell(self):
        class Kite:
            VARIETY_REGULAR = "regular"

            def __init__(self):
                self.order = None

            def place_order(self, **params):
                if params["order_type"] == "MARKET":
                    raise InputException("MARKET orders are blocked for stock options due to illiquidity")
                self.order = params
                return "LIMIT-ORDER"

            def quote(self, instruments):
                assert instruments == ["NFO:ILLQCE"]
                return {"NFO:ILLQCE": {"depth": {"buy": [{"price": 24.4}]}}}

        kite = Kite()
        order_id = place_market_order_with_depth_fallback(
            kite,
            variety="regular",
            exchange="NFO",
            tradingsymbol="ILLQCE",
            transaction_type=KiteConnect.TRANSACTION_TYPE_SELL,
            quantity=65,
            product="NRML",
            order_type=KiteConnect.ORDER_TYPE_MARKET,
        )

        self.assertEqual(order_id, "LIMIT-ORDER")
        self.assertEqual(kite.order["order_type"], KiteConnect.ORDER_TYPE_LIMIT)
        self.assertEqual(kite.order["price"], 24.4)


if __name__ == "__main__":
    unittest.main()
