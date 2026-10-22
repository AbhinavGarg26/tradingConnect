import unittest

from market.position_stops import PositionStopTracker, locked_profit_for_peak, position_pnl_pct


class PositionStopTrackerTests(unittest.TestCase):
    def test_short_profit_and_loss_are_inverted_from_price_movement(self):
        self.assertEqual(position_pnl_pct(100, 88, -65), 12.0)
        self.assertEqual(position_pnl_pct(100, 112, -65), -12.0)
    def test_only_hard_loss_exits_with_market_instruction(self):
        tracker = PositionStopTracker()
        self.assertIsNone(tracker.evaluate("NFO:X", -11.99, 12.0, []))
        self.assertEqual(
            tracker.evaluate("NFO:X", -12.0, 12.0, []),
            "HARD_STOP_12PCT",
        )

    def test_profit_target_has_been_removed(self):
        tracker = PositionStopTracker()
        self.assertIsNone(tracker.evaluate("NFO:X", 15.0, 12.0, []))

    def test_hard_loss_uses_configured_percentage(self):
        tracker = PositionStopTracker()
        self.assertIsNone(tracker.evaluate("NFO:X", -8.49, 8.5, []))
        self.assertEqual(tracker.evaluate("NFO:X", -8.5, 8.5, []), "HARD_STOP_8.5PCT")

    def test_max_loss_amount_exits_even_before_percentage_stop(self):
        tracker = PositionStopTracker()
        self.assertEqual(
            tracker.evaluate(
                "NFO:X", -4.0, 10.0, [], pnl_amount=-4000.0, max_loss_amount=4000.0
            ),
            "MAX_LOSS_4000INR",
        )

    def test_blank_max_loss_amount_skips_rupee_cap(self):
        tracker = PositionStopTracker()
        self.assertIsNone(
            tracker.evaluate("NFO:X", -4.0, 10.0, [], pnl_amount=-4000.0)
        )

    def test_ignored_position_keeps_hard_loss_but_never_uses_profit_ladder(self):
        tracker = PositionStopTracker()
        self.assertIsNone(
            tracker.evaluate_loss_only("NFO:X", 20.0, 10.0, pnl_amount=5000.0)
        )
        self.assertIsNone(
            tracker.evaluate_loss_only("NFO:X", 5.0, 10.0, pnl_amount=1250.0)
        )
        self.assertEqual(
            tracker.evaluate_loss_only("NFO:X", -10.0, 10.0, pnl_amount=-2500.0),
            "HARD_STOP_10PCT",
        )

    def test_ignored_position_also_uses_rupee_max_loss(self):
        tracker = PositionStopTracker()
        self.assertEqual(
            tracker.evaluate_loss_only(
                "NFO:X", -2.0, 10.0, pnl_amount=-4500.0, max_loss_amount=4500.0
            ),
            "MAX_LOSS_4500INR",
        )

    def test_profit_ladder_does_not_arm_before_ten_percent(self):
        tracker = PositionStopTracker()
        tracker.evaluate("NFO:X", 5.0, 5.8, [])
        reason = tracker.evaluate("NFO:X", 1.0, 5.8, [])
        self.assertIsNone(reason)
        snapshot = tracker.snapshot("NFO:X")
        self.assertIsNone(snapshot["locked_profit_pct"])

    def test_ten_percent_peak_uses_broker_gtt_not_software_floor(self):
        tracker = PositionStopTracker()
        tracker.evaluate("NFO:X", 10.0, 5.8, [])
        reason = tracker.evaluate("NFO:X", 2.5, 5.8, [])
        self.assertIsNone(reason)
        snapshot = tracker.snapshot("NFO:X")
        self.assertIsNone(snapshot["locked_profit_pct"])
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
        tracker.evaluate("NFO:X", 15.0, 5.8, [])
        tracker.evaluate("NFO:X", 12.0, 5.8, [])
        self.assertEqual(tracker.snapshot("NFO:X")["locked_profit_pct"], 5.0)


if __name__ == "__main__":
    unittest.main()
