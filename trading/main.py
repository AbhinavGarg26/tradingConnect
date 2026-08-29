"""
main.py — entry point for the Python trading engine.

Startup sequence:
  1. Load active user from DB
  2. LoginFlow.ensure_valid_session()  ← blocks if token expired, alerts via Telegram
  3. Start OrderPoller (background thread)
  4. Build StrategyEngine + OrderExecutor
  5. Start WebSocketEngine (blocking)

Run:
    python -m trading.main
"""

from __future__ import annotations

import logging
import signal
import sys

from dotenv import load_dotenv
load_dotenv()

from trading.database import get_db
from trading.login_flow import LoginFlow
from trading.order_executor import OrderExecutor
from trading.order_poller import OrderPoller
from trading.repositories import UserRepo
from trading.strategy_engine import StrategyEngine
from trading.websocket_engine import WebSocketEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("trading_engine.log"),
    ],
)
logger = logging.getLogger(__name__)


def main() -> None:
    logger.info("=" * 60)
    logger.info("Trading engine starting...")
    logger.info("=" * 60)

    # ── Step 1: Load user ─────────────────────────────────────
    with get_db() as db:
        user = UserRepo.get_active(db)
        if not user:
            logger.critical("No active user found in DB — exiting")
            sys.exit(1)
        user_id = user.id
        logger.info("User: %s (id=%s)", user.email, user_id)

    # ── Step 2: Validate session token ───────────────────────
    # Blocks here if token is expired — polls DB every 30s
    # Sends Telegram alert with login URL and waits up to 30 min
    login_flow = LoginFlow(user_id=user_id, timeout_minutes=30)
    try:
        login_flow.ensure_valid_session()
    except RuntimeError as exc:
        logger.critical("LoginFlow failed: %s — exiting", exc)
        sys.exit(1)

    logger.info("Session token valid — proceeding to start engine")

    # ── Step 3: Order poller (background thread) ─────────────
    poller = OrderPoller(user_id=user_id, interval_seconds=5)
    poller.start()

    # ── Step 4: Strategy + executor ──────────────────────────
    executor = OrderExecutor(user_id=user_id)
    strategy = StrategyEngine(user_id=user_id)
    strategy.on_signal(executor.handle)

    # ── Step 5: WebSocket engine (blocking) ──────────────────
    ws_engine = WebSocketEngine(user_id=user_id)
    ws_engine.register_strategy_handler(strategy.evaluate)

    def _shutdown(signum, frame):
        logger.info("Shutdown signal received — stopping engine...")
        poller.stop()
        ws_engine.stop()

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    logger.info("All systems go — connecting to Kite WebSocket")
    ws_engine.start()   # blocks until stop()
    logger.info("Trading engine stopped cleanly")


if __name__ == "__main__":
    main()
