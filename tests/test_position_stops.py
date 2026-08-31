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

    def test_profit_lock_uses_peak_and_never_moves_down(self):
        tracker = PositionStopTracker()
        self.assertIsNone(tracker.evaluate("NFO:X", 10.2, 5.8, [100, 105, 110], NOW))
        reason = tracker.evaluate("NFO:X", 4.9, 5.8, [110, 108, 105], NOW + timedelta(seconds=1))
        self.assertEqual(reason, "PROFIT_LOCK_5PCT")


if __name__ == "__main__":
    unittest.main()
