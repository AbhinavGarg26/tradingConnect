import time
import logging
import threading
import os
from datetime import datetime
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
from market.account_risk import AccountRiskMonitor
from market.recovery_gtt_executor import GrowwRecoveryGttMonitor, ZerodhaRecoveryGttExecutor

load_dotenv()

from trading.user_token import fetch_user_token
from trading.database import get_db
from trading.service_runtime import ServiceRuntimeMonitor

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
runtime_monitor = ServiceRuntimeMonitor(
    "market_breath", logger, token_alerts_during_market_session=True
)

ACTIVE_POLL_INTERVAL = 0.25
FINAL_SESSION_POLL_INTERVAL = 0.10
IDLE_POLL_INTERVAL = 2.0
FINAL_SESSION_START_HOUR = 15
BROKER_POSITION_REFRESH_INTERVAL = 1.0
PCT_LOSS = 5.5
KITE_AUTO_EXIT_ENABLED = os.getenv("KITE_AUTO_EXIT_ENABLED", "false").strip().lower() in {
    "1", "true", "yes", "on"
}
RECOVERY_GTT_ENABLED = os.getenv("RECOVERY_GTT_ENABLED", "false").strip().lower() in {
    "1", "true", "yes", "on"
}

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


def _position_poll_interval(position_count: int, now: datetime | None = None) -> float:
    """Poll fastest during the volatile final half-hour when positions are open."""
    if position_count == 0:
        return IDLE_POLL_INTERVAL
    current = now or datetime.now()
    if current.hour >= FINAL_SESSION_START_HOUR:
        return FINAL_SESSION_POLL_INTERVAL
    return ACTIVE_POLL_INTERVAL


def _warm_market_snapshots() -> None:
    """Warm analytics in the background; never delay live position protection."""
    try:
        background_kite, _user_id = fetch_user_token(logger)
        for interval, label in timeframe_mappings:
            with get_db() as db:
                sync_timeframe_snapshots(
                    background_kite,
                    db,
                    NIFTY_SYMBOL,
                    NIFTY_TOKEN,
                    interval=interval,
                    db_timeframe_label=label,
                )
    except BaseException as exc:
        logger.exception("Background market snapshot warmup failed: %s", exc)

if __name__ == "__main__":
    # Do not authenticate just because deploy.sh is run after market close or
    # on a weekend.  Keep the launched process idle so a deployment remains
    # healthy; it will authenticate only once the next session opens.
    while not is_market_open():
        logger.info("Market is closed. Position manager is waiting for the next session.")
        time.sleep(60)

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
    recovery_gtt_executor = ZerodhaRecoveryGttExecutor(kite, logger, user_id) if RECOVERY_GTT_ENABLED else None
    groww_recovery_monitor = GrowwRecoveryGttMonitor(logger, user_id) if RECOVERY_GTT_ENABLED else None
    entry_price_tracker = CurrentEntryPriceTracker()
    account_risk_monitor = AccountRiskMonitor(user_id, logger)
    price_stream.start()
    pos_count = 0
    last_live_state_sync = 0.0
    last_position_refresh = 0.0
    positions_response = None

    logger.info(
        "Starting Position Manager (%.2fs active, %.2fs final-session interval, auto exits %s, recovery GTTs %s)...",
        ACTIVE_POLL_INTERVAL,
        FINAL_SESSION_POLL_INTERVAL,
        "enabled" if KITE_AUTO_EXIT_ENABLED else "disabled",
        "enabled" if RECOVERY_GTT_ENABLED else "disabled",
    )
    threading.Thread(
        target=_warm_market_snapshots,
        name="market-snapshot-warmup",
        daemon=True,
    ).start()

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
                    active_symbols = trade_reconciler.run_if_due(kite, db, user_id)
                    now_monotonic = time.monotonic()
                    publish_live_state = now_monotonic - last_live_state_sync >= 2.0
                    if (
                        positions_response is None
                        or now_monotonic - last_position_refresh
                        >= BROKER_POSITION_REFRESH_INTERVAL
                    ):
                        positions_response = kite.positions()
                        last_position_refresh = now_monotonic

                # 2. Step 2: Recalculate summaries for updated symbols
                    if active_symbols:
                        for sym in active_symbols:
                            trigger_summary_updates(db, user_id=user_id, symbol=sym)

                        trigger_summary_updates(db, user_id=user_id, symbol="ALL")
                    account_risk_monitor.run_if_due(kite, db)
                    if groww_recovery_monitor is not None:
                        groww_recovery_monitor.run_if_due(db, now_monotonic)
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
                        auto_exit_enabled=KITE_AUTO_EXIT_ENABLED,
                        recovery_gtt_executor=recovery_gtt_executor,
                        positions_response=positions_response,
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

            time.sleep(_position_poll_interval(pos_count))
    finally:
        price_stream.stop()
        runtime_monitor.stop("Position manager stopped")
