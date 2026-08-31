import time
import logging
from dotenv import load_dotenv

from analytics.kite_sync_orders import process_and_merge_trades, trigger_summary_updates
from database.market_snapshot import sync_timeframe_snapshots
from market.candle_complete import CandleCompletionScheduler
from market.market import is_market_open
from market.market_exit import MarketExitExecutor
from market.market_positions import process_open_positions
from market.position_ltp_stream import PositionLtpStream
from market.position_stops import PositionStopTracker

load_dotenv()

from trading.user_token import fetch_user_token
from trading.database import get_db

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

POLL_INTERVAL = 0.5  # Time in seconds
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
    price_stream = PositionLtpStream(kite.api_key, kite.access_token)
    stop_tracker = PositionStopTracker()
    exit_executor = MarketExitExecutor(kite, logger)
    price_stream.start()
    pos_count = 0

    logger.info(f"Starting Position Manager with {POLL_INTERVAL}s interval...")

    for interval, label in timeframe_mappings:
        with get_db() as db:
            sync_timeframe_snapshots(kite, db, NIFTY_SYMBOL, NIFTY_TOKEN, interval=interval, db_timeframe_label=label)

    try:
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
                    pos_count = process_open_positions(
                        IGNORE_SYMBOL,
                        PCT_LOSS,
                        logger,
                        kite,
                        db,
                        price_stream,
                        stop_tracker,
                        exit_executor,
                    )

                    scheduler.check_and_sync(kite, db, NIFTY_SYMBOL, NIFTY_TOKEN)

                if not is_market_open():
                    logger.info("Market is closed. Program Halted...")
                    break

            except Exception as e:
                logger.exception("Error encountered during monitoring cycle: %s", e)

            interval = POLL_INTERVAL * (10 if pos_count == 0 else 1)
            time.sleep(interval)
    finally:
        price_stream.stop()
