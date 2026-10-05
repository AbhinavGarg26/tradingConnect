import unittest

from market.kite_ltp import live_prices, quote_key


class FakeKite:
    def __init__(self):
        self.keys = []

    def ltp(self, keys):
        self.keys = keys
        return {
            "NFO:NIFTY26O0622450PE": {"last_price": 109.5},
            "NSE:M&M": {"last_price": 3200.0},
        }


class KiteLtpTests(unittest.TestCase):
    def test_quote_key_maps_derivatives_to_kite_exchange(self):
        self.assertEqual(quote_key("NSE", "FNO", "NIFTY26O0622450PE"), "NFO:NIFTY26O0622450PE")
        self.assertEqual(quote_key("BSE", "FNO", "SENSEX26O0172400PE"), "BFO:SENSEX26O0172400PE")
        self.assertEqual(quote_key("NSE", "CASH", "M&M"), "NSE:M&M")

    def test_batches_groww_positions_through_kite(self):
        kite = FakeKite()
        prices = live_prices(kite, [
            {"exchange": "NSE", "segment": "FNO", "trading_symbol": "NIFTY26O0622450PE"},
            {"exchange": "NSE", "segment": "CASH", "trading_symbol": "M&M"},
        ])
        self.assertEqual(set(kite.keys), {"NFO:NIFTY26O0622450PE", "NSE:M&M"})
        self.assertEqual(prices[("NSE", "FNO", "NIFTY26O0622450PE")], 109.5)
        self.assertEqual(prices[("NSE", "CASH", "M&M")], 3200.0)
