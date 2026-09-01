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
        self.assertIsNone(
            tracker.evaluate("NFO:X", 9.6, 5.8, [110, 108, 105], NOW + timedelta(seconds=1))
        )
        self.assertIsNone(tracker.evaluate(
            "NFO:X", 9.6, 5.8, [110, 108, 105], NOW + timedelta(seconds=4)
        ))
        self.assertEqual(tracker.snapshot("NFO:X")["profit_ladder_stage"], 1)

    def test_first_profit_tier_locks_two_and_half_percent(self):
        tracker = PositionStopTracker()
        tracker.evaluate("NFO:X", 6.0, 5.8, [100, 106], NOW)
        # Confirm each floor and continue holding through the first three.
        for start, floor, expected_stage in (
            (1, 5.5, 1), (5, 4.5, 2), (9, 3.5, 3)
        ):
            self.assertIsNone(tracker.evaluate("NFO:X", floor, 5.8, [], NOW + timedelta(seconds=start)))
            self.assertIsNone(tracker.evaluate("NFO:X", floor, 5.8, [], NOW + timedelta(seconds=start + 3)))
            self.assertEqual(tracker.snapshot("NFO:X")["profit_ladder_stage"], expected_stage)
        self.assertIsNone(tracker.evaluate("NFO:X", 2.5, 5.8, [], NOW + timedelta(seconds=13)))
        reason = tracker.evaluate("NFO:X", 2.5, 5.8, [], NOW + timedelta(seconds=16))
        self.assertEqual(reason, "PROFIT_LOCK_2.5PCT")

    def test_soft_profit_lock_cancels_when_price_recovers(self):
        tracker = PositionStopTracker()
        tracker.evaluate("NFO:X", 6.0, 5.8, [100, 106], NOW)
        self.assertIsNone(
            tracker.evaluate("NFO:X", 5.4, 5.8, [106, 105.4], NOW + timedelta(seconds=1))
        )
        self.assertIsNone(
            tracker.evaluate("NFO:X", 5.6, 5.8, [105.4, 105.6], NOW + timedelta(seconds=3))
        )
        self.assertIsNone(
            tracker.evaluate("NFO:X", 5.4, 5.8, [105.6, 105.4], NOW + timedelta(seconds=7))
        )

    def test_profit_lock_hard_floor_exits_immediately(self):
        tracker = PositionStopTracker()
        tracker.evaluate("NFO:X", 6.0, 5.8, [100, 106], NOW)
        reason = tracker.evaluate(
            "NFO:X", 1.0, 5.8, [105.5, 101], NOW + timedelta(seconds=1)
        )
        self.assertEqual(reason, "PROFIT_LOCK_2.5PCT_HARD")


if __name__ == "__main__":
    unittest.main()
