import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from database.market_snapshot import (
    _aggregate_three_hour_candles,
    _aggregate_weekly_candles,
    _aggregate_monthly_candles,
    _completed_candles_only,
)


IST = ZoneInfo("Asia/Kolkata")


class MarketSnapshotCandleTests(unittest.TestCase):
    def test_daily_candles_are_aggregated_into_calendar_months(self):
        candles = pd.DataFrame({
            "date": pd.to_datetime(["2026-08-03", "2026-08-31", "2026-09-01"]),
            "open": [100, 104, 110], "high": [105, 109, 114], "low": [98, 103, 108],
            "close": [104, 108, 113], "volume": [10, 20, 30],
        })
        result = _aggregate_monthly_candles(candles)
        self.assertEqual(len(result), 2)
        self.assertEqual(result.iloc[0]["open"], 100)
        self.assertEqual(result.iloc[0]["close"], 108)
        self.assertEqual(result.iloc[0]["volume"], 30)

    def test_daily_candles_are_aggregated_into_monday_aligned_weeks(self):
        candles = pd.DataFrame({
            "date": pd.to_datetime(["2026-08-31", "2026-09-01", "2026-09-04", "2026-09-07"]),
            "open": [100, 102, 104, 110],
            "high": [103, 105, 108, 113],
            "low": [99, 101, 103, 109],
            "close": [102, 104, 107, 112],
            "volume": [10, 20, 30, 40],
        })

        result = _aggregate_weekly_candles(candles)

        self.assertEqual(len(result), 2)
        self.assertEqual(result.iloc[0]["open"], 100)
        self.assertEqual(result.iloc[0]["high"], 108)
        self.assertEqual(result.iloc[0]["close"], 107)
        self.assertEqual(result.iloc[0]["volume"], 60)

    def test_weekly_candle_completes_after_friday_close(self):
        candle = pd.DataFrame({"date": pd.to_datetime(["2026-08-31 09:15:00+05:30"])})

        before_close = _completed_candles_only(candle, "1w", datetime(2026, 9, 4, 15, 29, tzinfo=IST))
        after_close = _completed_candles_only(candle, "1w", datetime(2026, 9, 4, 15, 31, tzinfo=IST))

        self.assertTrue(before_close.empty)
        self.assertEqual(len(after_close), 1)

    def test_active_one_minute_candle_is_excluded(self):
        candles = pd.DataFrame({
            "date": pd.to_datetime([
                "2026-08-31 09:15:00+05:30",
                "2026-08-31 09:16:00+05:30",
            ])
        })

        result = _completed_candles_only(
            candles,
            "1m",
            datetime(2026, 8, 31, 9, 16, 30, tzinfo=IST),
        )

        self.assertEqual(result["date"].dt.strftime("%H:%M").tolist(), ["09:15"])

    def test_active_five_minute_candle_is_excluded(self):
        candle = pd.DataFrame({
            "date": pd.to_datetime(["2026-08-31 09:15:00+05:30"])
        })

        result = _completed_candles_only(
            candle,
            "5m",
            datetime(2026, 8, 31, 9, 19, 59, tzinfo=IST),
        )

        self.assertTrue(result.empty)

    def test_active_15_minute_candle_is_excluded(self):
        candles = pd.DataFrame({
            "date": pd.to_datetime([
                "2026-08-31 09:15:00+05:30",
                "2026-08-31 09:30:00+05:30",
            ])
        })

        result = _completed_candles_only(
            candles,
            "15m",
            datetime(2026, 8, 31, 9, 35, tzinfo=IST),
        )

        self.assertEqual(result["date"].dt.strftime("%H:%M").tolist(), ["09:15"])

    def test_candle_waits_for_finalization_grace(self):
        candle = pd.DataFrame({
            "date": pd.to_datetime(["2026-08-31 09:30:00+05:30"])
        })

        too_early = _completed_candles_only(
            candle,
            "15m",
            datetime(2026, 8, 31, 9, 45, 4, tzinfo=IST),
        )
        finalized = _completed_candles_only(
            candle,
            "15m",
            datetime(2026, 8, 31, 9, 45, 6, tzinfo=IST),
        )

        self.assertTrue(too_early.empty)
        self.assertEqual(len(finalized), 1)

    def test_three_hour_candles_are_anchored_to_nse_open(self):
        dates = pd.to_datetime([
            "2026-08-31 09:15:00+05:30",
            "2026-08-31 10:15:00+05:30",
            "2026-08-31 11:15:00+05:30",
            "2026-08-31 12:15:00+05:30",
            "2026-08-31 13:15:00+05:30",
            "2026-08-31 14:15:00+05:30",
            "2026-08-31 15:15:00+05:30",
        ])
        candles = pd.DataFrame({
            "date": dates,
            "open": [100, 101, 102, 103, 104, 105, 106],
            "high": [102, 103, 104, 105, 106, 107, 108],
            "low": [99, 100, 101, 102, 103, 104, 105],
            "close": [101, 102, 103, 104, 105, 106, 107],
            "volume": [10, 20, 30, 40, 50, 60, 70],
        })

        result = _aggregate_three_hour_candles(candles)

        self.assertEqual(
            result["date"].dt.strftime("%H:%M").tolist(),
            ["09:15", "12:15", "15:15"],
        )
        self.assertEqual(result["volume"].tolist(), [60, 150, 70])
        self.assertEqual(result["open"].tolist(), [100, 103, 106])
        self.assertEqual(result["close"].tolist(), [103, 106, 107])

    def test_final_partial_session_candle_closes_at_1530(self):
        candle = pd.DataFrame({
            "date": pd.to_datetime(["2026-08-31 15:15:00+05:30"])
        })

        result = _completed_candles_only(
            candle,
            "3h",
            datetime(2026, 8, 31, 15, 30, 6, tzinfo=IST),
        )

        self.assertEqual(len(result), 1)


if __name__ == "__main__":
    unittest.main()
