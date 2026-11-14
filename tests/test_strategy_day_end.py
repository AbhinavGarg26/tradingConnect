import unittest
from datetime import datetime

from strategy_day_end import IST, RESEARCH_REPLAY_KEYS, _as_trade, is_active, recent_session_dates


class StrategyDayEndTest(unittest.TestCase):
    def test_only_running_or_scheduled_strategies_are_active(self):
        self.assertTrue(is_active({"service_state": "active"}))
        self.assertTrue(is_active({"timer_state": "active", "timer_enabled": "enabled"}))
        self.assertFalse(is_active({"service_state": "inactive", "timer_state": "inactive",
                                    "timer_enabled": "disabled"}))

    def test_recent_sessions_are_distinct_and_limited_to_ten(self):
        rows = []
        for day in range(1, 16):
            rows.extend([{"date": datetime(2026, 10, day, 9, 15, tzinfo=IST)},
                         {"date": datetime(2026, 10, day, 15, 15, tzinfo=IST)}])
        result = recent_session_dates(rows)
        self.assertEqual(10, len(result))
        self.assertEqual(datetime(2026, 10, 6).date(), result[0])
        self.assertEqual(datetime(2026, 10, 15).date(), result[-1])

    def test_proxy_exit_is_derived_from_direction_and_points(self):
        put = _as_trade({"side": "PE", "date": "2026-10-15", "entry": 25000,
                         "points": 30, "exit_reason": "target"}, "fixed_profit_filtered")
        self.assertEqual(24970, put["exit"])

    def test_paper_research_replays_are_always_included(self):
        self.assertEqual({"one_direction_profit_lock"}, RESEARCH_REPLAY_KEYS)


if __name__ == "__main__":
    unittest.main()
