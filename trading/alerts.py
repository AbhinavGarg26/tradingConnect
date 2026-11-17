"""
alerts.py — Telegram operations alerts and persistent stock notifications.

Reads telegram_bot_token + telegram_chat_id from the market_configs table
(via MarketConfigRepo), scoped to the active Kite user.
Falls back to ENV vars if DB config is missing.

Usage:
    from trading.alerts import Alerter
    alerter = Alerter(bot_token="...", chat_id="...")
    alerter.send("WebSocket disconnected — reconnecting")
    alerter.send_async(message)   # fire-and-forget in a thread
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class Alerter:
    TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, bot_token: Optional[str] = None, chat_id: Optional[str] = None):
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id   = chat_id   or os.getenv("TELEGRAM_CHAT_ID",   "")
        self._enabled  = bool(self.bot_token and self.chat_id)

        if not self._enabled:
            logger.warning("Alerter: Telegram not configured; account-scoped stock inbox remains available")

    @classmethod
    def from_db(cls, db, user_id) -> "Alerter":
        """Build Alerter using tokens stored in system_configs."""
        from trading.repositories import MarketConfigRepo
        token   = MarketConfigRepo.get(db, user_id, "telegram_bot_token")
        chat_id = MarketConfigRepo.get(db, user_id, "telegram_chat_id")
        alerter = cls(bot_token=token, chat_id=chat_id)
        alerter.user_id = user_id
        alerter._notification_db = db
        return alerter

    def send(self, message: str, parse_mode: str = "HTML") -> bool:
        """
        Send a Telegram message synchronously.
        Returns True on success, False on failure (never raises).
        """
        if not self._enabled:
            logger.info("[ALERT] %s", message)
            return False

        try:
            url  = self.TELEGRAM_URL.format(token=self.bot_token)
            resp = httpx.post(
                url,
                json={"chat_id": self.chat_id, "text": message, "parse_mode": parse_mode},
                timeout=5,
            )
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.error("Telegram send failed: %s", exc)
            return False

    def send_async(self, message: str) -> None:
        """Fire-and-forget — does not block the WebSocket thread."""
        t = threading.Thread(target=self.send, args=(message,), daemon=True)
        t.start()

    # ── Pre-formatted alert templates ────────────────────────

    def ws_disconnected(self, reason: str = "") -> None:
        self.send_async(
            f"⚠️ <b>WebSocket disconnected</b>\n"
            f"Reason: {reason or 'unknown'}\n"
            f"Attempting reconnect..."
        )

    def ws_reconnected(self) -> None:
        self.send_async("✅ <b>WebSocket reconnected</b> — tick feed restored")

    def ws_failed(self, attempts: int) -> None:
        self.send_async(
            f"🚨 <b>WebSocket reconnect failed</b> after {attempts} attempts.\n"
            f"Manual intervention required."
        )

    def sl_trailed(self, symbol: str, old_sl: float, new_sl: float, method: str) -> None:
        self.send_async(
            f"📈 <b>SL Trailed</b> — {symbol}\n"
            f"Old SL: {old_sl}  →  New SL: {new_sl}\n"
            f"Method: {method}"
        )

    def sl_hit(self, symbol: str, sl_price: float, ltp: float) -> None:
        self.send_async(
            f"🔴 <b>SL Hit</b> — {symbol}\n"
            f"SL: {sl_price}  |  LTP: {ltp}"
        )

    def support_rejection(self, symbol: str, level: float) -> bool:
        from datetime import timezone
        return self._stock_notifications("support_rejection", [{"symbol": symbol, "reference": level}],
                                         datetime.now(timezone.utc))

    def order_rejected(self, symbol: str, reason: str) -> None:
        self.send_async(
            f"❌ <b>Order Rejected</b> — {symbol}\n"
            f"Reason: {reason}"
        )

    def volume_surges(self, surges: list[dict], captured_at: datetime) -> bool:
        return self._stock_notifications("volume_surge", surges, captured_at)

    def daily_volume_rvols(self, alerts: list[dict], captured_at: datetime,
                          market_close: bool = False) -> bool:
        items = [dict(alert, market_close=market_close) for alert in alerts]
        return self._stock_notifications("daily_volume", items, captured_at)

    def price_movements(self, movements: list[dict], captured_at: datetime) -> bool:
        return self._stock_notifications("price_movement", movements, captured_at)

    def proximity_alerts(self, alerts: list[dict], captured_at: datetime) -> bool:
        return self._stock_notifications("proximity", alerts, captured_at)

    def _stock_notifications(self, kind, items, captured_at) -> bool:
        """Persist stock alerts synchronously; Telegram credentials are irrelevant."""
        if not items:
            return False
        if getattr(self, "user_id", None) is None or getattr(self, "_notification_db", None) is None:
            logger.error("Stock inbox requires an account-scoped database session")
            return False
        try:
            from trading.notifications import persist_stock_notifications
            persist_stock_notifications(self._notification_db, self.user_id, kind, items, captured_at)
            return True
        except Exception:
            logger.exception("Stock inbox persistence failed")
            return False
