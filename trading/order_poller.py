"""
order_poller.py — polls Kite order book and syncs status to order_events.

Runs on a background thread every N seconds (configurable).
Detects COMPLETE / REJECTED status changes and triggers post-fill logic.

Why polling instead of WebSocket order updates:
  Kite's order update WebSocket is unreliable for missed events on reconnect.
  Polling the order book every few seconds is the safer production pattern.
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Optional

from trading.alerts import Alerter
from trading.database import get_db
from trading.exchange_link import ExchangeLinkRepo
from trading.models import OrderEvent
from trading.repositories import MarketConfigRepo, OrderEventRepo, TradeRepo
from trading.utils import is_market_hours

logger = logging.getLogger(__name__)


class OrderPoller:
    """
    Polls Kite order book every `interval_seconds` and syncs to DB.

    Usage:
        poller = OrderPoller(user_id=user_id, interval_seconds=5)
        poller.on_fill(callback)    # called when an order completes
        poller.start()              # non-blocking background thread
        poller.stop()               # graceful shutdown
    """

    def __init__(self, user_id: int, interval_seconds: int = 5):
        self.user_id          = user_id
        self.interval_seconds = interval_seconds
        self._running         = False
        self._thread:   Optional[threading.Thread] = None
        self._alerter:  Optional[Alerter]          = None
        self._fill_callbacks: list[Callable] = []
        self._initialised = False

    # ── Public API ────────────────────────────────────────────

    def on_fill(self, fn: Callable[[OrderEvent], None]) -> None:
        """Register callback invoked when an order reaches COMPLETE status."""
        self._fill_callbacks.append(fn)

    def start(self) -> None:
        """Start polling in a background daemon thread."""
        self._running = True
        self._thread  = threading.Thread(
            target=self._poll_loop, daemon=True, name="order-poller"
        )
        self._thread.start()
        logger.info("OrderPoller started — interval=%ds", self.interval_seconds)

    def stop(self) -> None:
        self._running = False
        logger.info("OrderPoller stopped")

    # ── Poll loop ─────────────────────────────────────────────

    def _poll_loop(self) -> None:
        if not self._initialised:
            self._load_config()

        while self._running:
            try:
                if is_market_hours():
                    self._sync_orders()
                else:
                    logger.debug("OrderPoller: market closed — skipping poll")
            except Exception as exc:
                logger.error("OrderPoller sync error: %s", exc, exc_info=True)
            threading.Event().wait(self.interval_seconds)

    def _sync_orders(self) -> None:
        try:
            with get_db() as db:
                kite = ExchangeLinkRepo.get_kite_client(db, self.user_id)
                orders = kite.orders()
        except Exception as exc:
            # Network error, token expired etc — log and skip this cycle
            logger.warning("OrderPoller: could not fetch orders — %s", exc)
            return

        if not orders:
            return

        with get_db() as db:
            for kite_order in orders:
                order_id = kite_order.get("order_id")
                status   = kite_order.get("status")
                if not order_id:
                    continue

                # Find matching order_event in DB
                from sqlalchemy import select
                existing: Optional[OrderEvent] = db.scalar(
                    select(OrderEvent).where(OrderEvent.kite_order_id == order_id)
                )
                if not existing:
                    continue   # order not tracked by our system

                old_status = existing.status
                if old_status == status:
                    continue   # no change

                # Update status
                existing.status          = status
                existing.status_message  = kite_order.get("status_message")
                existing.filled_quantity = Decimal(str(kite_order.get("filled_quantity", 0)))
                existing.average_price   = Decimal(str(kite_order.get("average_price", 0))) or None
                existing.updated_at      = kite_order.get("exchange_update_timestamp") or datetime.now(timezone.utc)

                logger.info(
                    "Order status updated — kite_order_id=%s %s → %s",
                    order_id, old_status, status,
                )

                # Fire fill callbacks
                if status == "COMPLETE":
                    self._on_order_complete(existing)

                elif status == "REJECTED":
                    msg = kite_order.get("status_message", "unknown")
                    logger.warning("Order REJECTED kite_order_id=%s: %s", order_id, msg)
                    self._alerter.send_async(
                        f"❌ <b>Order Rejected</b>\n"
                        f"Order ID: {order_id}\n"
                        f"Reason: {msg}"
                    )

    def _on_order_complete(self, event: OrderEvent) -> None:
        """Called when an order transitions to COMPLETE."""
        logger.info(
            "Order COMPLETE — kite_order_id=%s avg_price=%s qty=%s",
            event.kite_order_id, event.average_price, event.filled_quantity,
        )
        for fn in self._fill_callbacks:
            try:
                fn(event)
            except Exception as exc:
                logger.error("Fill callback error: %s", exc, exc_info=True)

    def _load_config(self) -> None:
        with get_db() as db:
            cfg = MarketConfigRepo.get_all(db, self.user_id)
        self._alerter = Alerter(
            bot_token=cfg.get("telegram_bot_token", ""),
            chat_id=cfg.get("telegram_chat_id", ""),
        )
        self._initialised = True