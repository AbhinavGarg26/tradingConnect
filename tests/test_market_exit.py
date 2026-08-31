import logging
import unittest

from market.market_exit import MarketExitExecutor


class FakeKite:
    TRANSACTION_TYPE_SELL = "SELL"
    VARIETY_REGULAR = "regular"
    ORDER_TYPE_MARKET = "MARKET"
    VALIDITY_DAY = "DAY"

    def __init__(self):
        self.placed = []
        self.order_book = []

    def get_gtts(self):
        return []

    def orders(self):
        return list(self.order_book)

    def positions(self):
        return {"net": [POSITION.copy()]}

    def place_order(self, **kwargs):
        self.placed.append(kwargs)
        return "ORDER1"


POSITION = {
    "exchange": "NFO",
    "tradingsymbol": "NIFTY26AUG25000CE",
    "product": "NRML",
    "instrument_token": 12345,
    "quantity": 75,
}


class MarketExitExecutorTests(unittest.TestCase):
    def test_market_exit_is_deduplicated_while_position_reconciles(self):
        kite = FakeKite()
        executor = MarketExitExecutor(kite, logging.getLogger("test"))

        first = executor.exit_position(POSITION, "EMERGENCY_STOP")
        second = executor.exit_position(POSITION, "EMERGENCY_STOP")

        self.assertEqual(first, "ORDER1")
        self.assertEqual(second, "ORDER1")
        self.assertEqual(len(kite.placed), 1)
        payload = kite.placed[0]
        self.assertEqual(payload["order_type"], "MARKET")
        self.assertEqual(payload["quantity"], 75)
        self.assertNotIn("market_protection", payload)


if __name__ == "__main__":
    unittest.main()
