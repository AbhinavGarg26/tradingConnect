import unittest

from market.recovery_gtt import RecoveryGttState, evaluate


class RecoveryGttTests(unittest.TestCase):
    def test_loss_events_are_armed_once_per_five_percent_level(self):
        state = RecoveryGttState()
        first = evaluate(100, 95, state)
        self.assertEqual((first.key, first.trigger_pct), ("loss-5", -1))
        self.assertIsNone(evaluate(100, 94, state))
        second = evaluate(100, 90, state)
        self.assertEqual((second.key, second.trigger_pct), ("loss-10", -6))

    def test_profit_event_waits_for_five_percent_drawdown_then_arms_recovery(self):
        state = RecoveryGttState()
        self.assertIsNone(evaluate(100, 110, state))
        event = evaluate(100, 105, state)
        self.assertEqual((event.key, event.trigger_pct), ("profit-10", 9))
        self.assertIsNone(evaluate(100, 105, state))

    def test_new_twenty_percent_peak_rearms_after_fifteen_percent_drawdown(self):
        state = RecoveryGttState()
        evaluate(100, 110, state)
        evaluate(100, 105, state)
        self.assertIsNone(evaluate(100, 120, state))
        event = evaluate(100, 115, state)
        self.assertEqual((event.key, event.trigger_pct), ("profit-20", 19))


if __name__ == "__main__":
    unittest.main()
