"""Thread-safe KiteTicker LTP stream for currently open positions."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import threading
from typing import Deque, Dict, Iterable, Optional

from kiteconnect import KiteTicker


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LivePrice:
    price: float
    received_at: datetime


class PositionLtpStream:
    """Maintain fresh LTP and short tick history for dynamic position tokens."""

    def __init__(self, api_key: str, access_token: str, history_size: int = 20):
        self._ticker = KiteTicker(api_key, access_token)
        self._ticker.on_ticks = self._on_ticks
        self._ticker.on_connect = self._on_connect
        self._ticker.on_close = self._on_close
        self._ticker.on_error = self._on_error
        self._lock = threading.RLock()
        self._target_tokens: set[int] = set()
        self._subscribed_tokens: set[int] = set()
        self._prices: Dict[int, LivePrice] = {}
        self._history: Dict[int, Deque[float]] = defaultdict(
            lambda: deque(maxlen=history_size)
        )
        self._connected = False

    def start(self) -> None:
        self._ticker.connect(threaded=True)

    def stop(self) -> None:
        try:
            self._ticker.close()
        except Exception:
            logger.exception("Failed to close position LTP stream")

    def update_tokens(self, tokens: Iterable[int]) -> None:
        target = {int(token) for token in tokens if token}
        with self._lock:
            self._target_tokens = target
            connected = self._connected
            to_add = target - self._subscribed_tokens
            to_remove = self._subscribed_tokens - target
        if not connected:
            return
        if to_add:
            token_list = list(to_add)
            self._ticker.subscribe(token_list)
            self._ticker.set_mode(self._ticker.MODE_LTP, token_list)
        if to_remove:
            self._ticker.unsubscribe(list(to_remove))
        with self._lock:
            self._subscribed_tokens.update(to_add)
            self._subscribed_tokens.difference_update(to_remove)
            for token in to_remove:
                self._prices.pop(token, None)
                self._history.pop(token, None)

    def get_price(self, token: int, max_age_seconds: float = 3.0) -> Optional[float]:
        with self._lock:
            live_price = self._prices.get(int(token))
        if live_price is None:
            return None
        age = (datetime.now(timezone.utc) - live_price.received_at).total_seconds()
        return live_price.price if age <= max_age_seconds else None

    def recent_prices(self, token: int, count: int = 5) -> list[float]:
        with self._lock:
            values = list(self._history.get(int(token), ()))
        return values[-count:]

    def record_rest_prices(self, prices: Dict[int, float]) -> None:
        """Seed freshness during startup or a temporary WebSocket data gap."""
        received_at = datetime.now(timezone.utc)
        with self._lock:
            for token, price in prices.items():
                if price <= 0:
                    continue
                token = int(token)
                self._prices[token] = LivePrice(price=float(price), received_at=received_at)
                self._history[token].append(float(price))

    def _on_connect(self, ws, response) -> None:
        with self._lock:
            self._connected = True
            tokens = list(self._target_tokens)
            self._subscribed_tokens.clear()
        if tokens:
            ws.subscribe(tokens)
            ws.set_mode(ws.MODE_LTP, tokens)
            with self._lock:
                self._subscribed_tokens.update(tokens)
        logger.info("Position LTP stream connected; subscribed to %d tokens", len(tokens))

    def _on_ticks(self, ws, ticks: list[dict]) -> None:
        received_at = datetime.now(timezone.utc)
        with self._lock:
            for tick in ticks:
                token = tick.get("instrument_token")
                price = tick.get("last_price")
                if token is None or price is None or float(price) <= 0:
                    continue
                token = int(token)
                price = float(price)
                self._prices[token] = LivePrice(price=price, received_at=received_at)
                self._history[token].append(price)

    def _on_close(self, ws, code, reason) -> None:
        with self._lock:
            self._connected = False
            self._subscribed_tokens.clear()
        logger.warning("Position LTP stream closed: code=%s reason=%s", code, reason)

    def _on_error(self, ws, code, reason) -> None:
        logger.error("Position LTP stream error: code=%s reason=%s", code, reason)
