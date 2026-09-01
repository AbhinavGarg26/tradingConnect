import unittest
from datetime import datetime, timedelta, timezone

from market.position_stops import PositionStopTracker


NOW = datetime(2026, 8, 31, 10, 0, tzinfo=timezone.utc)


class PositionStopTrackerTests(unittest.TestCase):
    def test_soft_breach_can_recover_with_hysteresis_and_momentum(self):
        tracker = PositionStopTracker()
        self.assertIsNone(tracker.evaluate("NFO:X", -5.8, 5.8, [100, 99, 98], NOW))
        self.assertIsNone(
            tracker.evaluate(
                "NFO:X", -4.9, 5.8, [98, 98.2, 98.1, 98.4, 98.7], NOW + timedelta(seconds=5)
            )
        )
        self.assertIsNone(
            tracker.evaluate("NFO:X", -5.8, 5.8, [100, 99, 98], NOW + timedelta(seconds=6))
        )

    def test_soft_breach_exits_after_timeout(self):
        tracker = PositionStopTracker()
        tracker.evaluate("NFO:X", -5.8, 5.8, [100, 99, 98], NOW)
        reason = tracker.evaluate(
            "NFO:X", -5.7, 5.8, [98, 98.1, 98], NOW + timedelta(seconds=15)
        )
        self.assertEqual(reason, "SOFT_STOP_TIMEOUT")

    def test_emergency_stop_does_not_wait(self):
        tracker = PositionStopTracker()
        reason = tracker.evaluate("NFO:X", -7.8, 5.8, [100, 95, 90], NOW)
        self.assertEqual(reason, "EMERGENCY_STOP")

    def test_profit_trail_uses_peak_and_never_moves_down(self):
        tracker = PositionStopTracker()
        self.assertIsNone(tracker.evaluate("NFO:X", 10.2, 5.8, [100, 105, 110], NOW))
        self.assertIsNone(
            tracker.evaluate("NFO:X", 7.6, 5.8, [110, 108, 105], NOW + timedelta(seconds=1))
        )
        reason = tracker.evaluate(
            "NFO:X", 7.6, 5.8, [110, 108, 105], NOW + timedelta(seconds=6)
        )
        self.assertEqual(reason, "PROFIT_TRAIL_2.5PCT")
        self.assertAlmostEqual(tracker.snapshot("NFO:X")["locked_profit_pct"], 7.7)

    def test_profit_mode_does_not_activate_before_eight_percent(self):
        tracker = PositionStopTracker()
        self.assertIsNone(tracker.evaluate("NFO:X", 5.9, 5.8, [], NOW))
        self.assertIsNone(tracker.snapshot("NFO:X")["locked_profit_pct"])

    def test_six_percent_peak_arms_charge_aware_floor(self):
        tracker = PositionStopTracker()
        tracker.evaluate("NFO:X", 6.0, 5.8, [], charge_floor_pct=3.48, now=NOW)
        snapshot = tracker.snapshot("NFO:X")
        self.assertAlmostEqual(snapshot["locked_profit_pct"], 3.48)
        self.assertTrue(snapshot["pre_profit_mode_active"])
        self.assertFalse(snapshot["profit_mode_active"])

        self.assertIsNone(tracker.evaluate(
            "NFO:X", 3.4, 5.8, [], charge_floor_pct=3.48,
            now=NOW + timedelta(seconds=1),
        ))
        reason = tracker.evaluate(
            "NFO:X", 3.4, 5.8, [], charge_floor_pct=3.48,
            now=NOW + timedelta(seconds=6),
        )
        self.assertEqual(reason, "PRE_PROFIT_CHARGE_FLOOR")

    def test_charge_floor_breach_is_cancelled_on_recovery(self):
        tracker = PositionStopTracker()
        tracker.evaluate("NFO:X", 7.0, 5.8, [], charge_floor_pct=3.5, now=NOW)
        tracker.evaluate(
            "NFO:X", 3.4, 5.8, [], charge_floor_pct=3.5,
            now=NOW + timedelta(seconds=1),
        )
        self.assertIsNone(tracker.evaluate(
            "NFO:X", 3.6, 5.8, [], charge_floor_pct=3.5,
            now=NOW + timedelta(seconds=4),
        ))
        self.assertIsNone(tracker.snapshot("NFO:X")["profit_breached_at"])

    def test_expensive_charge_floor_delays_pre_profit_activation(self):
        tracker = PositionStopTracker()
        tracker.evaluate("NFO:X", 6.0, 5.8, [], charge_floor_pct=6.14, now=NOW)
        self.assertIsNone(tracker.snapshot("NFO:X")["locked_profit_pct"])
        tracker.evaluate(
            "NFO:X", 6.64, 5.8, [], charge_floor_pct=6.14,
            now=NOW + timedelta(seconds=1),
        )
        self.assertAlmostEqual(tracker.snapshot("NFO:X")["locked_profit_pct"], 6.14)

    def test_profit_trail_confirmation_cancels_when_price_recovers(self):
        tracker = PositionStopTracker()
        tracker.evaluate("NFO:X", 10.0, 5.8, [100, 110], NOW)
        self.assertIsNone(
            tracker.evaluate("NFO:X", 7.4, 5.8, [110, 107.4], NOW + timedelta(seconds=1))
        )
        self.assertIsNone(
            tracker.evaluate("NFO:X", 7.6, 5.8, [107.4, 107.6], NOW + timedelta(seconds=4))
        )
        self.assertIsNone(
            tracker.evaluate("NFO:X", 7.4, 5.8, [107.6, 107.4], NOW + timedelta(seconds=7))
        )

    def test_new_peak_raises_trailing_floor(self):
        tracker = PositionStopTracker()
        tracker.evaluate("NFO:X", 8.0, 5.8, [], NOW)
        tracker.evaluate("NFO:X", 12.0, 5.8, [], NOW + timedelta(seconds=1))
        self.assertEqual(tracker.snapshot("NFO:X")["locked_profit_pct"], 9.5)

    def test_profit_hard_floor_exits_immediately(self):
        tracker = PositionStopTracker()
        tracker.evaluate("NFO:X", 8.0, 5.8, [100, 108], NOW)
        reason = tracker.evaluate(
            "NFO:X", 2.5, 5.8, [108, 102.5], NOW + timedelta(seconds=1)
        )
        self.assertEqual(reason, "PROFIT_TRAIL_2.5PCT_HARD_FLOOR")


if __name__ == "__main__":
    unittest.main()
