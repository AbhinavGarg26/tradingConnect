"""
login_flow.py — daily Kite session token management.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from trading.alerts import Alerter
from trading.database import get_db
from trading.exchange_link import ExchangeLink, ExchangeLinkRepo
from trading.repositories import MarketConfigRepo

logger = logging.getLogger(__name__)

TOKEN_POLL_INTERVAL = 30
KITE_LOGIN_URL = "https://kite.zerodha.com/connect/login?api_key={api_key}&v=3"


class LoginFlow:

    def __init__(self, user_id: uuid.UUID, timeout_minutes: int = 30):
        self.user_id         = user_id
        self.timeout_minutes = timeout_minutes
        self._alerter: Optional[Alerter] = None

    def ensure_valid_session(self) -> None:
        self._load_alerter()

        # Load everything needed INSIDE the session
        with get_db() as db:
            link = ExchangeLinkRepo.get_for_user(db, self.user_id)
            if not link:
                raise RuntimeError(
                    "No exchange_link found for user. "
                    "Create one via Python shell before starting the engine."
                )
            # Read all needed values while session is open
            is_valid    = link.is_session_valid
            api_key     = link.decrypt_access_id(db)
            expires_ist = link.session_expires_ist

        if is_valid:
            logger.info(
                "LoginFlow: session token valid — expires %s", expires_ist
            )
            return

        # Token expired or missing — alert and wait
        logger.warning("LoginFlow: session token expired or missing — waiting for refresh")
        self._send_login_alert(api_key)
        self._wait_for_token()

    # ── Alert ─────────────────────────────────────────────────

    def _send_login_alert(self, api_key: str) -> None:
        login_url  = KITE_LOGIN_URL.format(api_key=api_key)
        rails_url  = "http://localhost:3000/trading_session/new"

        message = (
            f"🔑 <b>Kite session token required</b>\n\n"
            f"The trading engine is waiting for a fresh session token.\n\n"
            f"<b>Steps:</b>\n"
            f"1. Open Kite login: <a href='{login_url}'>Login to Kite</a>\n"
            f"2. After login, copy the <code>request_token</code> from the redirect URL\n"
            f"3. Paste it here: <a href='{rails_url}'>Rails Token UI</a>\n\n"
            f"Engine will start automatically once token is saved.\n"
            f"Waiting up to {self.timeout_minutes} minutes..."
        )
        logger.info("LoginFlow: sending login alert via Telegram")
        self._alerter.send(message)

    # ── Poll until valid ──────────────────────────────────────

    def _wait_for_token(self) -> None:
        deadline = datetime.now(timezone.utc) + timedelta(minutes=self.timeout_minutes)
        attempt  = 0

        while datetime.now(timezone.utc) < deadline:
            attempt += 1
            time.sleep(TOKEN_POLL_INTERVAL)

            # Fresh DB query each poll — never reuse the old link object
            with get_db() as db:
                link = ExchangeLinkRepo.get_for_user(db, self.user_id)
                is_valid    = link.is_session_valid if link else False
                expires_ist = link.session_expires_ist if link else None

            if is_valid:
                logger.info(
                    "LoginFlow: valid token detected after %d poll(s) — engine starting",
                    attempt,
                )
                self._alerter.send(
                    f"✅ <b>Session token received</b>\n"
                    f"Trading engine starting now.\n"
                    f"Token expires: {expires_ist}"
                )
                return

            remaining = int((deadline - datetime.now(timezone.utc)).total_seconds() / 60)
            logger.info(
                "LoginFlow: still waiting for token (attempt %d, %d min remaining)",
                attempt, remaining,
            )

        # Timeout
        self._alerter.send(
            f"🚨 <b>Engine startup aborted</b>\n"
            f"No valid session token received within {self.timeout_minutes} minutes.\n"
            f"Restart the engine after pasting the token in Rails UI."
        )
        raise RuntimeError(
            f"LoginFlow timeout — no valid session token after {self.timeout_minutes} minutes"
        )

    def _load_alerter(self) -> None:
        with get_db() as db:
            cfg = MarketConfigRepo.get_all(db, self.user_id)
        self._alerter = Alerter(
            bot_token=cfg.get("telegram_bot_token", ""),
            chat_id=cfg.get("telegram_chat_id", ""),
        )