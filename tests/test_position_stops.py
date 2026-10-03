import unittest

from market.position_stops import PositionStopTracker, locked_profit_for_peak


class PositionStopTrackerTests(unittest.TestCase):
    def test_only_hard_loss_exits_with_market_instruction(self):
        tracker = PositionStopTracker()
        self.assertIsNone(tracker.evaluate("NFO:X", -11.99, 5.8, []))
        self.assertEqual(
            tracker.evaluate("NFO:X", -12.0, 5.8, []),
            "HARD_STOP_12PCT",
        )

    def test_fifteen_percent_profit_exits_immediately(self):
        tracker = PositionStopTracker()
        self.assertIsNone(tracker.evaluate("NFO:X", 14.99, 5.8, []))
        self.assertEqual(
            tracker.evaluate("NFO:X", 15.0, 5.8, []),
            "PROFIT_TARGET_15PCT",
        )

    def test_profit_ladder_does_not_arm_before_ten_percent(self):
        tracker = PositionStopTracker()
        tracker.evaluate("NFO:X", 5.0, 5.8, [])
        reason = tracker.evaluate("NFO:X", 1.0, 5.8, [])
        self.assertIsNone(reason)
        snapshot = tracker.snapshot("NFO:X")
        self.assertIsNone(snapshot["locked_profit_pct"])

    def test_ten_percent_peak_locks_two_and_a_half_percent(self):
        tracker = PositionStopTracker()
        tracker.evaluate("NFO:X", 10.0, 5.8, [])
        reason = tracker.evaluate("NFO:X", 2.5, 5.8, [])
        self.assertEqual(reason, "PROFIT_LADDER_STOP")
        snapshot = tracker.snapshot("NFO:X")
        self.assertEqual(snapshot["locked_profit_pct"], 2.5)
        self.assertIsNone(snapshot["profit_limit_target_pct"])

    def test_fifteen_percent_peak_locks_five_percent(self):
        tracker = PositionStopTracker()
        tracker.evaluate("NFO:X", 15.0, 5.8, [])
        self.assertEqual(
            tracker.evaluate("NFO:X", 5.0, 5.8, []),
            "PROFIT_LADDER_STOP",
        )

    def test_each_five_percent_peak_step_adds_two_and_a_half_before_fifty(self):
        self.assertEqual(locked_profit_for_peak(20.0), 7.5)
        self.assertEqual(locked_profit_for_peak(25.0), 10.0)
        self.assertEqual(locked_profit_for_peak(45.0), 20.0)

    def test_fifty_and_hundred_percent_overrides(self):
        self.assertEqual(locked_profit_for_peak(50.0), 40.0)
        self.assertEqual(locked_profit_for_peak(55.0), 42.5)
        self.assertEqual(locked_profit_for_peak(60.0), 45.0)
        self.assertEqual(locked_profit_for_peak(95.0), 62.5)
        self.assertEqual(locked_profit_for_peak(99.9), 62.5)
        self.assertEqual(locked_profit_for_peak(100.0), 92.5)
        self.assertEqual(locked_profit_for_peak(105.0), 95.0)

    def test_profit_floor_never_moves_down(self):
        tracker = PositionStopTracker()
        tracker.evaluate("NFO:X", 14.0, 5.8, [])
        tracker.evaluate("NFO:X", 12.0, 5.8, [])
        self.assertEqual(tracker.snapshot("NFO:X")["locked_profit_pct"], 2.5)


if __name__ == "__main__":
    unittest.main()
