import unittest

from market.market_positions import _atr_trail_distance_pct


class AtrTrailTests(unittest.TestCase):
    def test_uses_only_completed_one_minute_candles(self):
        candles = [
            {"high": 101, "low": 99, "close": 100, "is_complete": True},
            {"high": 103, "low": 100, "close": 102, "is_complete": True},
            {"high": 104, "low": 101, "close": 103, "is_complete": True},
            {"high": 120, "low": 80, "close": 90, "is_complete": False},
        ]
        self.assertAlmostEqual(_atr_trail_distance_pct(candles, 100), 4.0)

    def test_falls_back_until_three_candles_complete(self):
        candles = [
            {"high": 101, "low": 99, "close": 100, "is_complete": True},
            {"high": 102, "low": 100, "close": 101, "is_complete": True},
        ]
        self.assertIsNone(_atr_trail_distance_pct(candles, 100))


if __name__ == "__main__":
    unittest.main()
