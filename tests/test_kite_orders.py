import unittest

from market.kite_orders import place_protected_market_order


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


if __name__ == "__main__":
    unittest.main()
