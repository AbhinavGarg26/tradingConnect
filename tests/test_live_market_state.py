import unittest
from datetime import datetime, timezone
from decimal import Decimal

from database.live_market_state import _candle_payload, canonical_event_time
from market.position_ltp_stream import PositionLtpStream


class LiveMarketStateTests(unittest.TestCase):
    def test_stream_exposes_active_candle_for_all_live_timeframes(self):
        stream = PositionLtpStream("api-key", "access-token", permanent_tokens=[256265])
        stream._on_ticks(None, [{"instrument_token": 256265, "last_price": 25000.0}])
        stream._on_ticks(None, [{"instrument_token": 256265, "last_price": 25010.0}])

        for timeframe in (1, 3, 15):
            candles = stream.candle_snapshots(256265, timeframe)
            self.assertEqual(len(candles), 1)
            self.assertFalse(candles[0]["is_complete"])
            self.assertEqual(candles[0]["open"], 25000.0)
            self.assertEqual(candles[0]["close"], 25010.0)
            self.assertEqual(candles[0]["high"], 25010.0)

    def test_candle_identity_is_normalized_to_utc(self):
        ist = canonical_event_time("2026-08-31T09:30:00+05:30")
        utc = canonical_event_time("2026-08-31T04:00:00+00:00")
        self.assertEqual(ist, utc)
        self.assertEqual(ist.tzinfo, timezone.utc)

    def test_historical_current_candle_is_marked_unfinished(self):
        candle = {
            "date": "2026-08-31T09:30:00+05:30",
            "open": 100,
            "high": 102,
            "low": 99,
            "close": 101,
            "volume": 25,
        }
        payload, _ = _candle_payload(
            candle,
            timeframe=15,
            now=datetime(2026, 8, 31, 4, 5, tzinfo=timezone.utc),
        )
        self.assertFalse(payload["is_complete"])

    def test_seeded_partial_candle_keeps_earlier_ohlc(self):
        stream = PositionLtpStream("api-key", "access-token")
        stream.seed_current_candle({
            "instrument_token": 256265,
            "timeframe_minutes": 15,
            "open_time": "2026-08-31T04:00:00+00:00",
            "open": 100,
            "high": 105,
            "low": 98,
            "close": 102,
            "volume": 0,
        })
        stream._candle_builder.process_tick(
            256265,
            price=Decimal("103"),
            volume=0,
            ts=datetime(2026, 8, 31, 4, 6, tzinfo=timezone.utc),
        )
        candle = stream.candle_snapshots(256265, 15)[0]
        self.assertEqual(candle["open"], 100.0)
        self.assertEqual(candle["high"], 105.0)
        self.assertEqual(candle["low"], 98.0)
        self.assertEqual(candle["close"], 103.0)


if __name__ == "__main__":
    unittest.main()
