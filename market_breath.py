import time
import logging
from dotenv import load_dotenv

from analytics.kite_sync_orders import trigger_summary_updates
from analytics.trade_reconciliation import TradeReconciliationScheduler
from database.market_snapshot import sync_timeframe_snapshots
from database.live_market_state import (
    bootstrap_instrument_candles,
    sync_instrument_live_state,
)
from market.candle_complete import CandleCompletionScheduler
from market.market import is_market_open
from market.market_exit import MarketExitExecutor
from market.entry_price_tracker import CurrentEntryPriceTracker
from market.market_positions import process_open_positions
from market.position_ltp_stream import PositionLtpStream
from market.position_stops import PositionStopTracker

load_dotenv()

from trading.user_token import fetch_user_token
from trading.database import get_db
from trading.service_runtime import ServiceRuntimeMonitor

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
runtime_monitor = ServiceRuntimeMonitor("market_breath", logger)

POLL_INTERVAL = 0.5  # Time in seconds
PCT_LOSS = 5.8

IGNORE_SYMBOL = []

timeframe_mappings = [
            ("minute", "1m"),
            ("5minute", "5m"),
            ("15minute", "15m"),
            ("60minute", "1h"),
            ("60minute", "3h"),
            ("day", "1d"),
            ("day", "1w"),
            ("day", "1mo")
        ]

scheduler = CandleCompletionScheduler()
trade_reconciler = TradeReconciliationScheduler(interval_seconds=30)

NIFTY_SYMBOL = "NIFTY 50"
NIFTY_TOKEN = 256265

if __name__ == "__main__":
    runtime_monitor.start("Starting position manager")
    try:
        kite, user_id = fetch_user_token(logger)
    except BaseException as exc:
        runtime_monitor.exception(exc, "Creating Kite session")
        runtime_monitor.stop("Unable to create Kite session")
        raise
    price_stream = PositionLtpStream(
        kite.api_key,
        kite.access_token,
        permanent_tokens=[NIFTY_TOKEN],
    )
    stop_tracker = PositionStopTracker()
    exit_executor = MarketExitExecutor(kite, logger)
    entry_price_tracker = CurrentEntryPriceTracker()
    price_stream.start()
    pos_count = 0
    last_live_state_sync = 0.0

    logger.info(f"Starting Position Manager with {POLL_INTERVAL}s interval...")

    for interval, label in timeframe_mappings:
        with get_db() as db:
            sync_timeframe_snapshots(kite, db, NIFTY_SYMBOL, NIFTY_TOKEN, interval=interval, db_timeframe_label=label)

    try:
        with get_db() as db:
            unfinished_candles = bootstrap_instrument_candles(
                kite,
                db,
                entity_key="NSE:NIFTY 50",
                instrument_token=NIFTY_TOKEN,
            )
        for candle in unfinished_candles:
            price_stream.seed_current_candle(candle)
        logger.info("Bootstrapped latest 1m, 3m and 15m NIFTY candles")
    except Exception as exc:
        # Live position protection must continue even if the cache migration or
        # historical endpoint is temporarily unavailable.
        logger.exception("Live-state candle bootstrap failed: %s", exc)

    try:
        while True:
            try:

                with get_db() as db:
                    active_symbols = trade_reconciler.run_if_due(kite, db)
                    now_monotonic = time.monotonic()
                    publish_live_state = now_monotonic - last_live_state_sync >= 2.0

                # 2. Step 2: Recalculate summaries for updated symbols
                    if active_symbols:
                        for sym in active_symbols:
                            trigger_summary_updates(db, symbol=sym)

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
                        entry_price_tracker,
                        publish_live_state,
                    )

                    if publish_live_state:
                        try:
                            with db.begin_nested():
                                sync_instrument_live_state(
                                    db,
                                    price_stream,
                                    entity_key="NSE:NIFTY 50",
                                    instrument_token=NIFTY_TOKEN,
                                )
                        except Exception as exc:
                            logger.error("NIFTY live-state publish failed: %s", exc)
                        finally:
                            # Keep failure retries throttled as well, otherwise a
                            # missing migration can flood logs every 0.5 seconds.
                            last_live_state_sync = now_monotonic

                    scheduler.check_and_sync(kite, db, NIFTY_SYMBOL, NIFTY_TOKEN)
                    runtime_monitor.success("Position monitoring cycle completed")

                if not is_market_open():
                    logger.info("Market is closed. Program Halted...")
                    break

            except Exception as e:
                logger.exception("Error encountered during monitoring cycle: %s", e)
                runtime_monitor.exception(e, "Position monitoring cycle")

            interval = POLL_INTERVAL * (10 if pos_count == 0 else 1)
            time.sleep(interval)
    finally:
        price_stream.stop()
        runtime_monitor.stop("Position manager stopped")
