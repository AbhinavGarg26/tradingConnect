from datetime import datetime
import logging
from zoneinfo import ZoneInfo

from database.market_snapshot import sync_timeframe_snapshots

logger = logging.getLogger(__name__)

MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")
SYNC_GRACE_SECONDS = 5


class CandleCompletionScheduler:
    def __init__(self):
        # Tracking variables to ensure we only trigger ONCE per boundary minute
        self.last_triggered_15m = None
        self.last_triggered_1h = None
        self.last_triggered_3h = None

    def check_and_sync(self, kite, db, symbol: str, token: int):
        now = datetime.now(MARKET_TIMEZONE)
        current_minute_str = now.strftime("%Y-%m-%d %H:%M")

        minute = now.minute
        hour = now.hour

        # Check only between market hours (9:15 AM to 3:35 PM)
        if not (hour == 9 and minute >= 15) and not (9 < hour < 15) and not (hour == 15 and minute <= 35):
            return

        # -------------------------------------------------------------
        # 1. 15-MINUTE CANDLE CLOSING CHECK
        # Triggers at :00, :15, :30, :45 (e.g., 09:30, 09:45, 10:00, etc.)
        # -------------------------------------------------------------
        if minute % 15 == 0 and now.second >= SYNC_GRACE_SECONDS and self.last_triggered_15m != current_minute_str:
            logger.info(f"⏰ 15-Min Candle Closed at {current_minute_str}. Syncing...")
            sync_timeframe_snapshots(kite, db, symbol, token, interval="15minute", db_timeframe_label="15m")
            self.last_triggered_15m = current_minute_str

        # -------------------------------------------------------------
        # 2. 1-HOUR CANDLE CLOSING CHECK
        # Standard NSE 1H candles end at: 10:15, 11:15, 12:15, 13:15, 14:15, 15:15, 15:30
        # -------------------------------------------------------------
        is_1h_boundary = (minute == 15 and 10 <= hour <= 15) or (hour == 15 and minute == 30)
        if is_1h_boundary and now.second >= SYNC_GRACE_SECONDS and self.last_triggered_1h != current_minute_str:
            logger.info(f"⏰ 1-Hour Candle Closed at {current_minute_str}. Syncing...")
            sync_timeframe_snapshots(kite, db, symbol, token, interval="60minute", db_timeframe_label="1h")
            self.last_triggered_1h = current_minute_str

        # -------------------------------------------------------------
        # 3. 3-HOUR CANDLE CLOSING CHECK
        # NSE-session-aligned 3H candles end at 12:15 and 15:15.
        # The 15:15-15:30 remainder is a final partial session candle.
        # -------------------------------------------------------------
        is_3h_boundary = (minute == 15 and hour in [12, 15]) or (hour == 15 and minute == 30)
        if is_3h_boundary and now.second >= SYNC_GRACE_SECONDS and self.last_triggered_3h != current_minute_str:
            logger.info(f"⏰ 3-Hour Candle Closed at {current_minute_str}. Syncing...")
            sync_timeframe_snapshots(kite, db, symbol, token, interval="60minute", db_timeframe_label="3h")
            self.last_triggered_3h = current_minute_str
