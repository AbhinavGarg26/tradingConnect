"""
candle_builder.py — aggregates raw ticks into OHLCV candles.

Maintains an in-memory candle per instrument per timeframe.
Emits a closed candle dict when the timeframe boundary is crossed.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Dict, Optional


@dataclass
class Candle:
    instrument_token: int
    timeframe_minutes: int
    open:   Decimal
    high:   Decimal
    low:    Decimal
    close:  Decimal
    volume: int
    open_time:  datetime
    close_time: Optional[datetime] = None
    is_closed:  bool = False
    tick_count: int  = 1

    def update(self, price: Decimal, volume: int = 0) -> None:
        if price > self.high:
            self.high = price
        if price < self.low:
            self.low = price
        self.close   = price
        self.volume += volume
        self.tick_count += 1

    def to_dict(self) -> dict:
        return {
            "instrument_token": self.instrument_token,
            "timeframe":        self.timeframe_minutes,
            "open":             float(self.open),
            "high":             float(self.high),
            "low":              float(self.low),
            "close":            float(self.close),
            "volume":           self.volume,
            "open_time":        self.open_time.isoformat(),
            "close_time":       self.close_time.isoformat() if self.close_time else None,
            "tick_count":       self.tick_count,
        }


class CandleBuilder:
    """
    Aggregates ticks into candles for one or more timeframes.
    Thread-safe for a single writer (the WebSocket callback thread).

    Usage:
        builder = CandleBuilder(timeframes=[1, 5])
        builder.on_candle_close(callback)   # register handler
        builder.process_tick(token, price, volume, ts)
    """

    def __init__(self, timeframes: list[int] = None):
        # timeframes in minutes, e.g. [1, 5]
        self._timeframes: list[int] = timeframes or [1]
        # {instrument_token: {timeframe_minutes: Candle}}
        self._candles: Dict[int, Dict[int, Candle]] = defaultdict(dict)
        self._callbacks: list[Callable[[Candle], None]] = []

    def on_candle_close(self, fn: Callable[[Candle], None]) -> None:
        """Register a callback invoked with every closed Candle."""
        self._callbacks.append(fn)

    def process_tick(
        self,
        instrument_token: int,
        price: Decimal,
        volume: int,
        ts: datetime,
    ) -> None:
        for tf in self._timeframes:
            self._update(instrument_token, price, volume, ts, tf)

    def _update(
        self,
        token: int,
        price: Decimal,
        volume: int,
        ts: datetime,
        tf: int,
    ) -> None:
        candle = self._candles[token].get(tf)
        bucket = self._bucket_start(ts, tf)

        if candle is None:
            # First tick ever for this instrument + timeframe
            self._candles[token][tf] = self._new_candle(token, price, volume, bucket, tf)
            return

        if bucket > candle.open_time:
            # Crossed a candle boundary — close the old one, open a new one
            candle.close_time = ts
            candle.is_closed  = True
            self._emit(candle)
            self._candles[token][tf] = self._new_candle(token, price, volume, bucket, tf)
        else:
            candle.update(price, volume)

    def get_current(self, token: int, tf: int = 1) -> Optional[Candle]:
        return self._candles.get(token, {}).get(tf)

    def get_last_closed(self, token: int, tf: int = 1) -> Optional[Candle]:
        """Returns the most recently closed candle stored per instrument."""
        return self._last_closed.get((token, tf))

    # ── Internals ────────────────────────────────────────────

    def __init__(self, timeframes: list[int] = None):
        self._timeframes: list[int] = timeframes or [1]
        self._candles:      Dict[int, Dict[int, Candle]] = defaultdict(dict)
        self._last_closed:  Dict[tuple, Candle]          = {}
        self._callbacks:    list[Callable[[Candle], None]] = []

    @staticmethod
    def _bucket_start(ts: datetime, tf_minutes: int) -> datetime:
        """Floor ts to the nearest timeframe boundary."""
        total_minutes = ts.hour * 60 + ts.minute
        floored       = (total_minutes // tf_minutes) * tf_minutes
        return ts.replace(
            hour=floored // 60,
            minute=floored % 60,
            second=0,
            microsecond=0,
        )

    @staticmethod
    def _new_candle(token, price, volume, open_time, tf) -> Candle:
        return Candle(
            instrument_token=token,
            timeframe_minutes=tf,
            open=price, high=price, low=price, close=price,
            volume=volume,
            open_time=open_time,
        )

    def _emit(self, candle: Candle) -> None:
        self._last_closed[(candle.instrument_token, candle.timeframe_minutes)] = candle
        for fn in self._callbacks:
            try:
                fn(candle)
            except Exception as exc:
                import logging
                logging.getLogger(__name__).error(
                    "Candle callback error: %s", exc, exc_info=True
                )