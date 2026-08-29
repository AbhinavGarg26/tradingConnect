"""
candle_store.py — in-memory rolling window of closed candles per instrument.

The strategy engine needs recent candle history to compute:
  - ATR (14-period by default)
  - Swing highs / lows (last N candles)
  - EMA values
  - Support rejection confirmation (prev candle low vs current close)

CandleStore is populated by WebSocketEngine via on_candle_close callbacks.
It is NOT persisted to DB — it rebuilds from ticks on every engine restart.
"""

from __future__ import annotations

from collections import defaultdict, deque
from decimal import Decimal
from typing import Deque, Dict, List, Optional

from trading.candle_builder import Candle


class CandleStore:
    """
    Rolling window of the last N closed candles per (instrument_token, timeframe).

    Usage:
        store = CandleStore(maxlen=100)
        store.push(candle)                         # called from on_candle_close
        candles = store.get(token, tf=1)           # list of Candle, oldest first
        atr     = ATRCalculator.compute(candles)
    """

    def __init__(self, maxlen: int = 100):
        self._maxlen = maxlen
        # {(instrument_token, timeframe_minutes): deque[Candle]}
        self._store: Dict[tuple, Deque[Candle]] = defaultdict(
            lambda: deque(maxlen=self._maxlen)
        )

    def push(self, candle: Candle) -> None:
        key = (candle.instrument_token, candle.timeframe_minutes)
        self._store[key].append(candle)

    def get(self, token: int, tf: int = 1) -> List[Candle]:
        """Returns candles oldest-first. Empty list if none yet."""
        return list(self._store[(token, tf)])

    def last(self, token: int, tf: int = 1, n: int = 1) -> List[Candle]:
        """Returns the last n candles, most-recent last."""
        candles = self.get(token, tf)
        return candles[-n:] if len(candles) >= n else candles

    def count(self, token: int, tf: int = 1) -> int:
        return len(self._store[(token, tf)])

    def has_enough(self, token: int, tf: int = 1, minimum: int = 14) -> bool:
        return self.count(token, tf) >= minimum


class ATRCalculator:
    """
    Wilder's ATR (Average True Range).
    Uses simple mean for the first period, then Wilder smoothing.
    """

    @staticmethod
    def true_range(prev_close: Decimal, high: Decimal, low: Decimal) -> Decimal:
        return max(
            high - low,
            abs(high - prev_close),
            abs(low  - prev_close),
        )

    @classmethod
    def compute(cls, candles: List[Candle], period: int = 14) -> Optional[Decimal]:
        """
        Returns ATR for the most recent candle.
        Requires at least period + 1 candles.
        Returns None if insufficient data.
        """
        if len(candles) < period + 1:
            return None

        # True ranges
        trs: List[Decimal] = []
        for i in range(1, len(candles)):
            tr = cls.true_range(
                candles[i - 1].close,
                Decimal(str(candles[i].high)),
                Decimal(str(candles[i].low)),
            )
            trs.append(tr)

        # First ATR = simple mean of first `period` TRs
        atr = sum(trs[:period]) / period

        # Wilder smoothing for subsequent values
        for tr in trs[period:]:
            atr = (atr * (period - 1) + tr) / period

        return atr.quantize(Decimal("0.0001"))

    @classmethod
    def compute_latest(
        cls,
        store: CandleStore,
        token: int,
        tf: int = 1,
        period: int = 14,
    ) -> Optional[Decimal]:
        candles = store.get(token, tf)
        return cls.compute(candles, period)


class EMACalculator:
    """Exponential Moving Average."""

    @staticmethod
    def compute(candles: List[Candle], period: int) -> Optional[Decimal]:
        """Returns EMA of close prices for the most recent candle."""
        if len(candles) < period:
            return None

        closes = [c.close for c in candles]
        k      = Decimal("2") / (period + 1)

        # Seed with simple mean of first `period` closes
        ema = sum(closes[:period]) / period

        for price in closes[period:]:
            ema = price * k + ema * (1 - k)

        return ema.quantize(Decimal("0.01"))

    @classmethod
    def compute_latest(
        cls,
        store: CandleStore,
        token: int,
        tf: int = 1,
        period: int = 21,
    ) -> Optional[Decimal]:
        candles = store.get(token, tf)
        return cls.compute(candles, period)


class SwingCalculator:
    """Swing high / low detection over a lookback window."""

    @staticmethod
    def last_swing_high(candles: List[Candle], lookback: int = 5) -> Optional[Decimal]:
        """
        Highest high in the last `lookback` candles.
        Used as trailing SL reference for SELL trades.
        """
        if len(candles) < lookback:
            return None
        window = candles[-lookback:]
        return max(Decimal(str(c.high)) for c in window)

    @staticmethod
    def last_swing_low(candles: List[Candle], lookback: int = 5) -> Optional[Decimal]:
        """
        Lowest low in the last `lookback` candles.
        Used as trailing SL reference for BUY trades.
        """
        if len(candles) < lookback:
            return None
        window = candles[-lookback:]
        return min(Decimal(str(c.low)) for c in window)

    @classmethod
    def compute(
        cls,
        store: CandleStore,
        token: int,
        direction: str,
        tf: int = 1,
        lookback: int = 5,
    ) -> Optional[Decimal]:
        candles = store.get(token, tf)
        if direction == "BUY":
            return cls.last_swing_low(candles, lookback)
        return cls.last_swing_high(candles, lookback)