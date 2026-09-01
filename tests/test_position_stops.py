import unittest

from market.position_stops import PositionStopTracker


class PositionStopTrackerTests(unittest.TestCase):
    def test_only_hard_loss_exits_with_market_instruction(self):
        tracker = PositionStopTracker()
        self.assertIsNone(tracker.evaluate("NFO:X", -5.8, 5.8, []))
        self.assertEqual(
            tracker.evaluate("NFO:X", -7.8, 5.8, []),
            "EMERGENCY_STOP",
        )

    def test_five_percent_peak_places_four_percent_limit_after_two_percent_pullback(self):
        tracker = PositionStopTracker()
        tracker.evaluate("NFO:X", 5.0, 5.8, [])
        reason = tracker.evaluate("NFO:X", 2.0, 5.8, [])
        self.assertEqual(reason, "PROFIT_5PCT_RECOVERY_LIMIT")
        snapshot = tracker.snapshot("NFO:X")
        self.assertEqual(snapshot["locked_profit_pct"], 2.0)
        self.assertEqual(snapshot["profit_limit_target_pct"], 4.0)

    def test_ten_percent_peak_places_seven_percent_limit_after_four_percent_pullback(self):
        tracker = PositionStopTracker()
        tracker.evaluate("NFO:X", 10.0, 5.8, [])
        reason = tracker.evaluate("NFO:X", 4.0, 5.8, [])
        self.assertEqual(reason, "PROFIT_10PCT_RECOVERY_LIMIT")
        snapshot = tracker.snapshot("NFO:X")
        self.assertEqual(snapshot["locked_profit_pct"], 4.0)
        self.assertEqual(snapshot["profit_limit_target_pct"], 7.0)

    def test_ten_percent_peak_permanently_arms_two_percent_hard_floor(self):
        tracker = PositionStopTracker()
        tracker.evaluate("NFO:X", 10.0, 5.8, [])
        self.assertEqual(
            tracker.evaluate("NFO:X", 2.0, 5.8, []),
            "PROFIT_HARD_FLOOR",
        )

    def test_above_ten_percent_uses_atr_and_targets_four_points_above_trigger(self):
        tracker = PositionStopTracker()
        tracker.evaluate("NFO:X", 12.0, 5.8, [], atr_trail_distance_pct=4.0)
        reason = tracker.evaluate(
            "NFO:X", 8.0, 5.8, [], atr_trail_distance_pct=4.0
        )
        self.assertEqual(reason, "PROFIT_ATR_RECOVERY_LIMIT")
        snapshot = tracker.snapshot("NFO:X")
        self.assertEqual(snapshot["locked_profit_pct"], 8.0)
        self.assertEqual(snapshot["profit_limit_target_pct"], 12.0)
        self.assertTrue(snapshot["atr_trail_active"])

    def test_atr_floor_never_moves_down(self):
        tracker = PositionStopTracker()
        tracker.evaluate("NFO:X", 15.0, 5.8, [], atr_trail_distance_pct=4.0)
        tracker.evaluate("NFO:X", 14.0, 5.8, [], atr_trail_distance_pct=6.0)
        self.assertEqual(tracker.snapshot("NFO:X")["locked_profit_pct"], 11.0)


if __name__ == "__main__":
    unittest.main()
