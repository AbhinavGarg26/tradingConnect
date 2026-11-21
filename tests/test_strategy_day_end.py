import unittest
from datetime import date, datetime
from decimal import Decimal

from strategy_day_end import (IST, RESEARCH_REPLAY_KEYS, VERSIONS, _as_trade,
                              _replay_groww_option_trade, _replay_profit_lock_option_trade,
                              is_active, recent_session_dates)


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
        self.assertEqual({"one_direction_profit_lock", "smallcap_momentum_swing", "rsi_highway",
                          "gap_up_volume_confirmed"},
                         RESEARCH_REPLAY_KEYS)

    def test_equity_paper_versions_are_pinned(self):
        self.assertEqual("v2-first-15m-strength-confirmation", VERSIONS["smallcap_momentum_swing"])
        self.assertEqual("v2-rising-weekly-confirmation", VERSIONS["rsi_highway"])
        self.assertEqual("v1-prior-volume-15m-confirmed", VERSIONS["gap_up_volume_confirmed"])

    def test_groww_orb_paper_version_is_pinned(self):
        self.assertEqual("v1-pre1100-directional-paper", VERSIONS["groww_orb"])

    def test_profit_lock_option_replay_uses_actual_ten_percent_loss_stop(self):
        replay = _replay_profit_lock_option_trade(
            _OptionKite([
                _option_bar(9, 30, 100, 102, 99),
                _option_bar(9, 31, 95, 96, 89),
                _option_bar(15, 15, 80, 81, 79),
            ]), [_contract()], _signal())
        self.assertEqual("stop", replay["reason"])
        self.assertEqual(Decimal("90.05"), replay["initial_stop"])
        self.assertEqual(Decimal("89.95"), replay["exit_premium"])
        self.assertEqual(Decimal("-507.50"), replay["gross_option_pnl"])

    def test_profit_lock_option_replay_locks_actual_ten_percent_profit(self):
        replay = _replay_profit_lock_option_trade(
            _OptionKite([
                _option_bar(9, 30, 100, 101, 99),
                _option_bar(9, 31, 108, 112, 107),
                _option_bar(9, 32, 104, 105, 103),
                _option_bar(15, 15, 80, 81, 79),
            ]), [_contract()], _signal())
        self.assertEqual("stop", replay["reason"])
        self.assertEqual(Decimal("103.90"), replay["exit_premium"])
        self.assertEqual(Decimal("190.00"), replay["gross_option_pnl"])

    def test_groww_orb_option_replay_uses_near_expiry_atm_and_adverse_fills(self):
        signal = dict(_signal(), reason="session_end")
        replay = _replay_groww_option_trade(
            _OptionKite([
                _option_bar(9, 30, 100, 101, 99),
                _option_bar(15, 15, 110, 111, 109),
            ]), [_contract()], signal)
        self.assertEqual("NIFTY26N1925000PE", replay["option_symbol"])
        self.assertEqual(Decimal("100.10"), replay["entry_premium"])
        self.assertEqual(Decimal("109.90"), replay["exit_premium"])
        self.assertEqual(Decimal("490.00"), replay["gross_option_pnl"])


class _OptionKite:
    def __init__(self, candles):
        self.candles = candles

    def historical_data(self, *_args):
        return self.candles


def _option_bar(hour, minute, open_price, high, low):
    return {"date": datetime(2026, 11, 19, hour, minute, tzinfo=IST), "open": open_price,
            "high": high, "low": low, "close": open_price}


def _contract():
    return {"name": "NIFTY", "instrument_type": "PE", "expiry": date(2026, 11, 26),
            "strike": 25000, "instrument_token": 123, "tradingsymbol": "NIFTY26N1925000PE",
            "tick_size": 0.05, "lot_size": 50}


def _signal():
    return {"side": "PE", "entry_time": datetime(2026, 11, 19, 9, 30, tzinfo=IST),
            "exit_time": datetime(2026, 11, 19, 15, 15, tzinfo=IST), "entry": Decimal("25000"),
            "exit": Decimal("24900"), "points": Decimal("100"), "stop": None, "target": None,
            "reason": "session_end"}


if __name__ == "__main__":
    unittest.main()
