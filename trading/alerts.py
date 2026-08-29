"""
alerts.py — Telegram notification helper.

Reads bot_token + chat_id from system_configs table (via MarketConfigRepo).
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
            logger.warning("Alerter: Telegram not configured — alerts will only be logged")

    @classmethod
    def from_db(cls, db, user_id) -> "Alerter":
        """Build Alerter using tokens stored in system_configs."""
        from trading.repositories import MarketConfigRepo
        token   = MarketConfigRepo.get(db, user_id, "telegram_bot_token")
        chat_id = MarketConfigRepo.get(db, user_id, "telegram_chat_id")
        return cls(bot_token=token, chat_id=chat_id)

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

    def support_rejection(self, symbol: str, level: float) -> None:
        self.send_async(
            f"🔔 <b>Support Rejection</b> — {symbol}\n"
            f"Price rejected from support @ {level}"
        )

    def order_rejected(self, symbol: str, reason: str) -> None:
        self.send_async(
            f"❌ <b>Order Rejected</b> — {symbol}\n"
            f"Reason: {reason}"
        )