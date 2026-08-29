"""
websocket_engine.py — Kite WebSocket tick listener.

Responsibilities:
  - Subscribe to all active instruments from DB
  - Build 1-min (and configurable) candles from ticks
  - Trigger trailing SL logic on candle close
  - Reconnect with exponential backoff on disconnect
  - Alert via Telegram on disconnect / reconnect / failure

Architecture:
  KiteTicker (Kite thread)
      │
      ├── on_ticks()     → CandleBuilder.process_tick()
      │                       │
      │                       └── on_candle_close() → StrategyDispatcher.evaluate()
      │
      ├── on_connect()   → subscribe all active tokens
      ├── on_close()     → schedule reconnect + Telegram alert
      └── on_error()     → log + Telegram alert

Usage:
    engine = WebSocketEngine(user_id=uuid)
    engine.start()       # blocking — run in main thread or dedicated process
    engine.stop()        # graceful shutdown
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, List, Optional

from kiteconnect import KiteTicker

from trading.alerts import Alerter
from trading.candle_builder import Candle, CandleBuilder
from trading.database import get_db
from trading.exchange_link import ExchangeLinkRepo
from trading.repositories import MarketConfigRepo, InstrumentRepo, TradeRepo
from trading.utils import is_market_hours

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# TickData — normalised tick
# ─────────────────────────────────────────────────────────────

class TickData:
    """Thin wrapper around a raw Kite tick dict."""

    __slots__ = (
        "instrument_token", "ltp", "volume", "oi",
        "bid", "ask", "timestamp",
    )

    def __init__(self, raw: dict):
        self.instrument_token: int            = raw["instrument_token"]
        self.ltp:              Decimal        = Decimal(str(raw.get("last_price", 0)))
        self.volume:           int            = raw.get("volume_traded", 0) or 0
        self.oi:               int            = raw.get("oi", 0) or 0
        self.bid:              Optional[Decimal] = (
            Decimal(str(raw["depth"]["buy"][0]["price"]))
            if raw.get("depth") else None
        )
        self.ask:              Optional[Decimal] = (
            Decimal(str(raw["depth"]["sell"][0]["price"]))
            if raw.get("depth") else None
        )
        self.timestamp: datetime = raw.get("exchange_timestamp") or datetime.now(timezone.utc)

    def __repr__(self) -> str:
        return f"<Tick {self.instrument_token} ltp={self.ltp}>"


# ─────────────────────────────────────────────────────────────
# StrategyDispatcher — called on every closed candle
# ─────────────────────────────────────────────────────────────

class StrategyDispatcher:
    """
    Receives closed candles and dispatches to trailing SL evaluation.
    This is the bridge between the WebSocket engine and the strategy engine.
    Kept intentionally thin — strategy logic lives in strategy_engine.py.
    """

    def __init__(self, user_id: int, alerter: Alerter):
        self.user_id = user_id
        self.alerter = alerter
        self._handlers: List[Callable[[Candle, uuid.UUID], None]] = []

    def register(self, handler: Callable[[Candle, uuid.UUID], None]) -> None:
        """Register a strategy handler. Called with (candle, user_id)."""
        self._handlers.append(handler)

    def evaluate(self, candle: Candle) -> None:
        """Called by CandleBuilder on every candle close."""
        if not self._handlers:
            return
        for handler in self._handlers:
            try:
                handler(candle, self.user_id)
            except Exception as exc:
                logger.error(
                    "Strategy handler error on token=%s candle=%s: %s",
                    candle.instrument_token, candle.open_time, exc,
                    exc_info=True,
                )


# ─────────────────────────────────────────────────────────────
# WebSocketEngine
# ─────────────────────────────────────────────────────────────

class WebSocketEngine:

    # Reconnect config
    MAX_RECONNECT_ATTEMPTS = 10
    RECONNECT_BASE_DELAY   = 2    # seconds — doubles each attempt (exponential backoff)
    RECONNECT_MAX_DELAY    = 120  # cap at 2 minutes

    def __init__(self, user_id: int):
        self.user_id    = user_id
        self._ticker:   Optional[KiteTicker] = None
        self._tokens:   List[int]            = []
        self._running   = False
        self._reconnect_attempts = 0
        self._reconnect_timer:  Optional[threading.Timer] = None
        self._stop_event = threading.Event()

        # Sub-components (initialised in start())
        self._alerter:    Optional[Alerter]            = None
        self._builder:    Optional[CandleBuilder]      = None
        self._dispatcher: Optional[StrategyDispatcher] = None

        # Configurable timeframe — loaded from system_configs
        self._trigger_tf: int = 1   # minutes, default 1

    # ── Public API ────────────────────────────────────────────

    def register_strategy_handler(
        self,
        handler: Callable[[Candle, uuid.UUID], None],
    ) -> None:
        """
        Register a function called on every closed candle.
        Call before start(). Signature: handler(candle, user_id)
        """
        if self._dispatcher is None:
            self._pending_handlers = getattr(self, "_pending_handlers", [])
            self._pending_handlers.append(handler)
        else:
            self._dispatcher.register(handler)

    def start(self) -> None:
        """
        Start the WebSocket engine. Blocking call.
        Run in a dedicated thread or process.
        """
        logger.info("WebSocketEngine: starting for user_id=%s", self.user_id)
        self._running = True
        self._initialise()
        self._connect()
        # Block until stop() is called
        self._stop_event.wait()
        logger.info("WebSocketEngine: stopped")

    def stop(self) -> None:
        """Graceful shutdown."""
        logger.info("WebSocketEngine: stopping...")
        self._running = False
        self._stop_event.set()
        if self._reconnect_timer:
            self._reconnect_timer.cancel()
        if self._ticker:
            try:
                self._ticker.stop()
            except Exception:
                pass

    def get_ltp(self, instrument_token: int) -> Optional[Decimal]:
        """Last traded price from the in-memory candle builder."""
        candle = self._builder.get_current(instrument_token, self._trigger_tf)
        return candle.close if candle else None

    def subscribed_tokens(self) -> List[int]:
        return list(self._tokens)

    # ── Initialisation ────────────────────────────────────────

    def _initialise(self) -> None:
        with get_db() as db:
            # Load config
            cfg = MarketConfigRepo.get_all(db, self.user_id)
            self._trigger_tf = int(cfg.get("trail_trigger_tf", 1))

            # Build alerter from system_configs
            self._alerter = Alerter(
                bot_token=cfg.get("telegram_bot_token", ""),
                chat_id=cfg.get("telegram_chat_id", ""),
            )

            # Load all active instrument tokens
            self._tokens = InstrumentRepo.get_active_tokens(db)
            logger.info("WebSocketEngine: loaded %d instrument tokens", len(self._tokens))

        # Candle builder — builds at trigger timeframe
        self._builder = CandleBuilder(timeframes=[self._trigger_tf])

        # Strategy dispatcher
        self._dispatcher = StrategyDispatcher(self.user_id, self._alerter)
        for handler in getattr(self, "_pending_handlers", []):
            self._dispatcher.register(handler)

        # Wire candle close → dispatcher
        self._builder.on_candle_close(self._dispatcher.evaluate)

    def _build_ticker(self) -> KiteTicker:
        # Load everything needed INSIDE the session — never let the object escape
        with get_db() as db:
            link = ExchangeLinkRepo.get_for_user(db, self.user_id)
            if not link or not link.is_session_valid:
                raise RuntimeError("Exchange link session is expired or missing")
            # Decrypt while session is still open
            api_key = link.decrypt_access_id(db)
            access_token = link.decrypt_session_token(db)

        # Now build ticker with plain strings — no SQLAlchemy objects outside session
        ticker = KiteTicker(api_key, access_token)
        ticker.on_ticks = self._on_ticks
        ticker.on_connect = self._on_connect
        ticker.on_close = self._on_close
        ticker.on_error = self._on_error
        ticker.on_reconnect = self._on_reconnect
        ticker.on_noreconnect = self._on_no_reconnect
        return ticker

    def _connect(self) -> None:
        try:
            self._ticker = self._build_ticker()
            # reconnect=False — we handle reconnect ourselves
            self._ticker.connect(threaded=True)
        except Exception as exc:
            logger.error("WebSocketEngine: connect failed — %s", exc)
            self._schedule_reconnect()

    # ── KiteTicker callbacks ──────────────────────────────────

    def _on_connect(self, ws, response) -> None:
        logger.info("WebSocketEngine: connected")
        self._reconnect_attempts = 0

        if not self._tokens:
            logger.warning("WebSocketEngine: no tokens to subscribe")
            return

        # Subscribe in batches of 3000 (Kite limit)
        for i in range(0, len(self._tokens), 3000):
            batch = self._tokens[i : i + 3000]
            ws.subscribe(batch)
            ws.set_mode(ws.MODE_FULL, batch)
            logger.info("WebSocketEngine: subscribed %d tokens (batch %d)", len(batch), i // 3000 + 1)

        if self._reconnect_attempts > 0:
            self._alerter.ws_reconnected()

    def _on_ticks(self, ws, ticks: list) -> None:
        for raw in ticks:
            try:
                tick = TickData(raw)
                self._builder.process_tick(
                    instrument_token=tick.instrument_token,
                    price=tick.ltp,
                    volume=tick.volume,
                    ts=tick.timestamp,
                )
            except Exception as exc:
                logger.debug("Tick parse error token=%s: %s", raw.get("instrument_token"), exc)

    def _on_close(self, ws, code, reason):
        if self._running:
            if is_market_hours():
                self._alerter.ws_disconnected(reason=str(reason or code))
                self._schedule_reconnect()
            else:
                logger.info("WebSocketEngine: market closed — not reconnecting")

    def _on_error(self, ws, code, reason) -> None:
        logger.error("WebSocketEngine: error — code=%s reason=%s", code, reason)
        if self._running:
            self._alerter.send_async(
                f"⚠️ <b>WebSocket error</b>\nCode: {code}\nReason: {reason}"
            )

    def _on_reconnect(self, ws, attempts_count) -> None:
        logger.info("WebSocketEngine: Kite internal reconnect attempt %d", attempts_count)

    def _on_no_reconnect(self, ws, attempts_count) -> None:
        logger.warning("WebSocketEngine: Kite gave up reconnecting after %d attempts", attempts_count)
        if self._running:
            self._schedule_reconnect()

    # ── Reconnect with exponential backoff ────────────────────

    def _schedule_reconnect(self) -> None:
        if not self._running:
            return

        self._reconnect_attempts += 1

        if self._reconnect_attempts > self.MAX_RECONNECT_ATTEMPTS:
            logger.critical(
                "WebSocketEngine: max reconnect attempts (%d) reached — giving up",
                self.MAX_RECONNECT_ATTEMPTS,
            )
            self._alerter.ws_failed(self._reconnect_attempts)
            self.stop()
            return

        delay = min(
            self.RECONNECT_BASE_DELAY * (2 ** (self._reconnect_attempts - 1)),
            self.RECONNECT_MAX_DELAY,
        )
        logger.info(
            "WebSocketEngine: reconnect attempt %d/%d in %.0fs",
            self._reconnect_attempts, self.MAX_RECONNECT_ATTEMPTS, delay,
        )

        if self._ticker:
            try:
                self._ticker.stop()
            except Exception:
                pass
            self._ticker = None

        self._reconnect_timer = threading.Timer(delay, self._connect)
        self._reconnect_timer.daemon = True
        self._reconnect_timer.start()

    # ── Token management (hot-reload without restart) ─────────

    def reload_tokens(self) -> None:
        """
        Reload active tokens from DB and update subscriptions live.
        Call this after adding a new instrument to the DB.
        """
        if not self._ticker:
            return
        with get_db() as db:
            new_tokens = set(InstrumentRepo.get_active_tokens(db))

        old_tokens  = set(self._tokens)
        to_add      = new_tokens - old_tokens
        to_remove   = old_tokens - new_tokens

        if to_add:
            tokens_list = list(to_add)
            self._ticker.subscribe(tokens_list)
            self._ticker.set_mode(self._ticker.MODE_FULL, tokens_list)
            logger.info("WebSocketEngine: subscribed %d new tokens", len(to_add))

        if to_remove:
            self._ticker.unsubscribe(list(to_remove))
            logger.info("WebSocketEngine: unsubscribed %d tokens", len(to_remove))

        self._tokens = list(new_tokens)