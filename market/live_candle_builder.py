"""Small single-writer OHLC builder for live-state WebSocket ticks."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Callable, Dict, Optional


@dataclass
class LiveCandle:
    instrument_token: int
    timeframe_minutes: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    open_time: datetime
    close_time: Optional[datetime] = None
    is_closed: bool = False
    tick_count: int = 1

    def update(self, price: Decimal, volume: int = 0) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.volume += volume
        self.tick_count += 1

    def to_dict(self) -> dict:
        return {
            "instrument_token": self.instrument_token,
            "timeframe": self.timeframe_minutes,
            "open": float(self.open),
            "high": float(self.high),
            "low": float(self.low),
            "close": float(self.close),
            "volume": self.volume,
            "open_time": self.open_time.isoformat(),
            "close_time": self.close_time.isoformat() if self.close_time else None,
            "tick_count": self.tick_count,
        }


class LiveCandleBuilder:
    def __init__(self, timeframes: list[int]):
        self._timeframes = timeframes
        self._candles: Dict[int, Dict[int, LiveCandle]] = defaultdict(dict)
        self._callbacks: list[Callable[[LiveCandle], None]] = []

    def on_candle_close(self, callback: Callable[[LiveCandle], None]) -> None:
        self._callbacks.append(callback)

    def process_tick(self, instrument_token: int, price: Decimal, volume: int, ts: datetime) -> None:
        for timeframe in self._timeframes:
            bucket = self._bucket_start(ts, timeframe)
            candle = self._candles[instrument_token].get(timeframe)
            if candle is None:
                self._candles[instrument_token][timeframe] = self._new_candle(
                    instrument_token, price, volume, bucket, timeframe
                )
            elif bucket > candle.open_time:
                candle.close_time = bucket
                candle.is_closed = True
                for callback in self._callbacks:
                    callback(candle)
                self._candles[instrument_token][timeframe] = self._new_candle(
                    instrument_token, price, volume, bucket, timeframe
                )
            else:
                candle.update(price, volume)

    def get_current(self, instrument_token: int, timeframe: int) -> Optional[LiveCandle]:
        return self._candles.get(instrument_token, {}).get(timeframe)

    def seed_current(self, candle: LiveCandle) -> None:
        """Seed an unfinished REST candle before WebSocket ticks take over."""
        existing = self._candles[candle.instrument_token].get(candle.timeframe_minutes)
        if existing is None or candle.open_time > existing.open_time:
            self._candles[candle.instrument_token][candle.timeframe_minutes] = candle
        elif candle.open_time == existing.open_time:
            # The socket may already have received ticks while REST history was
            # loading. Preserve the historical open/range and the fresher close.
            existing.open = candle.open
            existing.high = max(existing.high, candle.high)
            existing.low = min(existing.low, candle.low)
            existing.volume = max(existing.volume, candle.volume)
            existing.tick_count += candle.tick_count

    @staticmethod
    def _bucket_start(ts: datetime, timeframe: int) -> datetime:
        total_minutes = ts.hour * 60 + ts.minute
        floored = (total_minutes // timeframe) * timeframe
        return ts.replace(
            hour=floored // 60,
            minute=floored % 60,
            second=0,
            microsecond=0,
        )

    @staticmethod
    def _new_candle(token, price, volume, open_time, timeframe) -> LiveCandle:
        return LiveCandle(
            instrument_token=token,
            timeframe_minutes=timeframe,
            open=price,
            high=price,
            low=price,
            close=price,
            volume=volume,
            open_time=open_time,
        )
