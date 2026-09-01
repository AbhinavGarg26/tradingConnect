import unittest

from market.entry_price_tracker import CurrentEntryPriceTracker


class FakeKite:
    def __init__(self, trades):
        self._trades = trades
        self.calls = 0

    def trades(self):
        self.calls += 1
        return self._trades


def fill(order_id, side, price, timestamp):
    return {
        "exchange": "NFO", "tradingsymbol": "NIFTYTESTCE", "product": "NRML",
        "transaction_type": side, "order_id": order_id, "quantity": 65,
        "average_price": price, "fill_timestamp": timestamp,
    }


class EntryPriceTrackerTests(unittest.TestCase):
    def test_resolves_latest_fifo_open_leg_not_day_buy_average(self):
        trades = [
            fill("B1", "BUY", 71.5, "2026-09-01T09:16:25"),
            fill("S1", "SELL", 74.7, "2026-09-01T09:28:07"),
            fill("B2", "BUY", 70.5, "2026-09-01T09:41:48"),
            fill("S2", "SELL", 64.8, "2026-09-01T09:43:40"),
            fill("B3", "BUY", 62.0, "2026-09-01T09:45:00"),
        ]
        position = {
            "exchange": "NFO", "tradingsymbol": "NIFTYTESTCE", "product": "NRML",
            "quantity": 65, "buy_quantity": 195, "buy_value": 13260,
            "sell_quantity": 130, "sell_value": 9067.5,
        }
        kite = FakeKite(trades)
        tracker = CurrentEntryPriceTracker()

        price, changed = tracker.resolve(kite, position)
        cached_price, cached_changed = tracker.resolve(kite, position)

        self.assertEqual(price, 62.0)
        self.assertFalse(changed)
        self.assertEqual(cached_price, 62.0)
        self.assertFalse(cached_changed)
        self.assertEqual(kite.calls, 1)


if __name__ == "__main__":
    unittest.main()
