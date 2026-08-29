import time
from datetime import datetime
import logging
from dotenv import load_dotenv

from analytics.kite_sync_orders import process_and_merge_trades, trigger_summary_updates
from database.market_snapshot import sync_timeframe_snapshots
from market.candle_complete import CandleCompletionScheduler
from market.market import is_market_open
from market.market_positions import process_open_positions

load_dotenv()

from trading.user_token import fetch_user_token
from trading.database import get_db

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

today = datetime.now()
POLL_INTERVAL = 0.5  # Time in seconds
if today.weekday() == 4:
    PCT_LOSS = 3.8
else:
    PCT_LOSS = 5.8

IGNORE_SYMBOL = []

timeframe_mappings = [
            ("15minute", "15m"),
            ("60minute", "1h"),
            ("60minute", "3h")
        ]

scheduler = CandleCompletionScheduler()

NIFTY_SYMBOL = "NIFTY 50"
NIFTY_TOKEN = 256265

if __name__ == "__main__":
    kite, user_id = fetch_user_token(logger)

    logger.info(f"Starting Position Manager with {POLL_INTERVAL}s interval...")

    for interval, label in timeframe_mappings:
        with get_db() as db:
            sync_timeframe_snapshots(kite, db, NIFTY_SYMBOL, NIFTY_TOKEN, interval=interval, db_timeframe_label=label)

    while True:
        try:

            with get_db() as db:
                active_symbols = process_and_merge_trades(kite, db)

                # 2. Step 2: Recalculate summaries for updated symbols
                if active_symbols:
                    for sym in active_symbols:
                        trigger_summary_updates(db, symbol=sym)

                # Always update the combined overall portfolio summary ("ALL")
                trigger_summary_updates(db, symbol="ALL")
                pos_count = process_open_positions(IGNORE_SYMBOL, PCT_LOSS, logger, kite, db)

                scheduler.check_and_sync(kite, db, NIFTY_SYMBOL, NIFTY_TOKEN)

            if is_market_open():
                pass
            else:
                logger.info("Market is closed. Program Halted...")
                exit()

        except Exception as e:
            logger.error(f"Error encountered during monitoring cycle: {e}")

        # Pause execution before the next run
        interval = POLL_INTERVAL
        if pos_count == 0:
            interval = interval * 10

        time.sleep(interval)
