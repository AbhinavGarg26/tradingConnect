"""Paper-first NIFTY intraday scalper signal engine.

Signals use completed five-minute NIFTY index bars. The engine never places an
order. ``nearest_weekly_atm_for_signal`` maps an entry signal to the nearest
listed unexpired NIFTY expiry and ATM CE/PE; a runner must still confirm the
actual fill, enforce rupee risk, and journal every decision.
"""
from dataclasses import dataclass
from datetime import time, timedelta
from decimal import Decimal

from strategies.nifty_opening_range import IST, current_atm_option

KEY = "nifty_scalper_5m"
VERSION = "v1-research-disabled"


@dataclass(frozen=True)
class ScalpSignal:
    action: str
    time: object
    index_price: Decimal
    reason: str
    stop_distance: Decimal | None = None


def nearest_weekly_atm_for_signal(instruments, today, signal):
    """Return the nearest-expiry ATM contract matching the entry direction."""
    if signal is None or signal.action not in {"BUY_CE", "BUY_PE"}:
        raise ValueError("A BUY_CE or BUY_PE scalper signal is required")
    side = signal.action.removeprefix("BUY_")
    return current_atm_option(instruments, today, side, signal.index_price)


class NiftyScalper:
    """EMA pullback continuation with bounded ATR risk and session locks."""

    def __init__(self, *, fast_period=5, slow_period=13, atr_period=10,
                 atr_multiplier=1, min_stop=8, max_stop=18,
                 reward_multiple=Decimal("1.5"), max_entries=3,
                 max_losses=2, daily_loss_points=30, cooldown_minutes=15,
                 entry_start=time(10, 15), entry_cutoff=time(14, 30),
                 forced_exit=time(15, 15), min_ema_atr=0,
                 min_body_fraction=0, min_close_location=0,
                 max_extension_atr=Decimal("10"), require_slow_slope=False,
                 allowed_sides=("CE", "PE")):
        if not (2 <= fast_period < slow_period):
            raise ValueError("EMA periods must satisfy 2 <= fast < slow")
        if atr_period < 2 or max_entries < 1 or max_losses < 1:
            raise ValueError("ATR, entry and loss limits must be positive")
        self.fast_period, self.slow_period, self.atr_period = fast_period, slow_period, atr_period
        self.atr_multiplier = self._positive(atr_multiplier, "ATR multiplier")
        self.min_stop = self._positive(min_stop, "minimum stop")
        self.max_stop = self._positive(max_stop, "maximum stop")
        self.reward_multiple = self._positive(reward_multiple, "reward multiple")
        self.daily_loss_points = self._positive(daily_loss_points, "daily loss points")
        self.min_ema_atr = Decimal(str(min_ema_atr))
        self.min_body_fraction = Decimal(str(min_body_fraction))
        self.min_close_location = Decimal(str(min_close_location))
        self.max_extension_atr = self._positive(max_extension_atr, "maximum extension ATR")
        if not all(v.is_finite() and 0 <= v <= 1 for v in
                   (self.min_ema_atr, self.min_body_fraction, self.min_close_location)):
            raise ValueError("Entry filter fractions must be finite values from zero to one")
        self.require_slow_slope = bool(require_slow_slope)
        self.allowed_sides = frozenset(allowed_sides)
        if not self.allowed_sides or not self.allowed_sides <= {"CE", "PE"}:
            raise ValueError("Allowed sides must contain CE, PE, or both")
        if self.min_stop > self.max_stop:
            raise ValueError("Minimum stop cannot exceed maximum stop")
        self.max_entries, self.max_losses = max_entries, max_losses
        self.cooldown = timedelta(minutes=cooldown_minutes)
        self.entry_start, self.entry_cutoff, self.forced_exit = entry_start, entry_cutoff, forced_exit
        self.reset()

    @staticmethod
    def _positive(value, label):
        value = Decimal(str(value))
        if not value.is_finite() or value <= 0:
            raise ValueError(f"{label.capitalize()} must be positive and finite")
        return value

    def reset(self):
        self.session_date = None
        self.bars = []
        self.fast = self.slow = self.atr = None
        self.slow_history = []
        self.pending = None
        self.position = None
        self.entry = self.stop = self.target = None
        self.entries = self.losses = 0
        self.session_points = Decimal("0")
        self.lock_reason = None
        self.cooldown_until = None
        self.last_bar_end = None

    @property
    def can_enter(self):
        return self.lock_reason is None and not self.position and not self.pending and self.entries < self.max_entries

    def on_bar(self, bar, *, observed_at):
        self._validate_bar(bar)
        end = bar.time + timedelta(minutes=5)
        if observed_at.tzinfo is None or observed_at < end:
            raise ValueError("Use only a completed five-minute bar")
        day = bar.time.astimezone(IST).date()
        if self.session_date != day:
            self.reset(); self.session_date = day
        if self.last_bar_end and bar.time < self.last_bar_end:
            raise ValueError("Bars must be chronological and non-overlapping")
        self.last_bar_end = end

        previous = self.bars[-1] if self.bars else None
        prior_fast = self.fast
        self.bars.append(bar)
        self.fast = self._ema(self.fast, bar.close, self.fast_period)
        self.slow = self._ema(self.slow, bar.close, self.slow_period)
        self.slow_history.append(self.slow)
        self.atr = self._next_atr()
        local_time = end.astimezone(IST).time()
        if (not self.can_enter or len(self.bars) < self.slow_period or self.atr is None or
                local_time < self.entry_start or local_time > self.entry_cutoff or
                (self.cooldown_until and end < self.cooldown_until)):
            return None

        stop_distance = min(self.max_stop, max(self.min_stop, self.atr * self.atr_multiplier))
        candle_range = bar.high - bar.low
        body_fraction = abs(bar.close - bar.open) / candle_range if candle_range else Decimal("0")
        bullish_close = (bar.close - bar.low) / candle_range if candle_range else Decimal("0")
        bearish_close = (bar.high - bar.close) / candle_range if candle_range else Decimal("0")
        separation_ok = abs(self.fast - self.slow) / self.atr >= self.min_ema_atr
        slow_rising = len(self.slow_history) >= 2 and self.slow_history[-1] > self.slow_history[-2]
        slow_falling = len(self.slow_history) >= 2 and self.slow_history[-1] < self.slow_history[-2]
        bullish = (self.fast > self.slow and previous and prior_fast is not None and
                   previous.low <= prior_fast <= previous.high and bar.close > previous.high and
                   bar.close > bar.open and "CE" in self.allowed_sides and separation_ok and
                   body_fraction >= self.min_body_fraction and bullish_close >= self.min_close_location and
                   bar.close - previous.high <= self.atr * self.max_extension_atr and
                   (not self.require_slow_slope or slow_rising))
        bearish = (self.fast < self.slow and previous and prior_fast is not None and
                   previous.low <= prior_fast <= previous.high and bar.close < previous.low and
                   bar.close < bar.open and "PE" in self.allowed_sides and separation_ok and
                   body_fraction >= self.min_body_fraction and bearish_close >= self.min_close_location and
                   previous.low - bar.close <= self.atr * self.max_extension_atr and
                   (not self.require_slow_slope or slow_falling))
        if not bullish and not bearish:
            return None
        self.pending = ScalpSignal("BUY_CE" if bullish else "BUY_PE", end, bar.close,
                                   "ema_pullback_breakout", stop_distance)
        return self.pending

    def confirm_entry(self, *, index_price):
        if not self.pending or not self.pending.action.startswith("BUY_"):
            raise ValueError("No pending entry")
        price = self._positive(index_price, "entry price")
        side = "CE" if self.pending.action == "BUY_CE" else "PE"
        direction = Decimal("1") if side == "CE" else Decimal("-1")
        self.position, self.entry = side, price
        self.stop = price - direction * self.pending.stop_distance
        self.target = price + direction * self.pending.stop_distance * self.reward_multiple
        self.entries += 1
        self.pending = None

    def reject_entry(self):
        self.pending = None

    def on_tick(self, timestamp, index_price):
        if not self.position:
            return None
        if timestamp.tzinfo is None:
            raise ValueError("Timezone-aware tick required")
        price = self._positive(index_price, "index price")
        direction = Decimal("1") if self.position == "CE" else Decimal("-1")
        progress = direction * (price - self.entry)
        risk = abs(self.entry - self.stop)
        if progress >= risk and direction * (self.stop - self.entry) < 0:
            self.stop = self.entry
        reason = None
        if timestamp.astimezone(IST).time() >= self.forced_exit:
            reason = "session_end"
        elif direction * (price - self.stop) <= 0:
            reason = "stop"
        elif direction * (price - self.target) >= 0:
            reason = "target"
        if reason:
            self.pending = ScalpSignal(f"EXIT_{self.position}", timestamp, price, reason)
        return self.pending

    def confirm_exit(self, *, index_price, timestamp):
        if not self.position or not self.pending or not self.pending.action.startswith("EXIT_"):
            raise ValueError("No pending exit")
        price = self._positive(index_price, "exit price")
        direction = Decimal("1") if self.position == "CE" else Decimal("-1")
        points = direction * (price - self.entry)
        self.session_points += points
        self.losses = self.losses + 1 if points < 0 else 0
        self.position = self.entry = self.stop = self.target = self.pending = None
        self.cooldown_until = timestamp + self.cooldown
        if self.session_points <= -self.daily_loss_points:
            self.lock_reason = "daily_loss"
        elif self.losses >= self.max_losses:
            self.lock_reason = "consecutive_losses"
        elif self.entries >= self.max_entries:
            self.lock_reason = "max_entries"
        return points

    @staticmethod
    def _ema(previous, value, period):
        value = Decimal(value)
        if previous is None:
            return value
        alpha = Decimal("2") / Decimal(period + 1)
        return value * alpha + previous * (Decimal("1") - alpha)

    def _next_atr(self):
        if len(self.bars) < 2:
            return None
        sample = self.bars[-self.atr_period:]
        if len(sample) < self.atr_period:
            return None
        true_ranges = []
        start = len(self.bars) - len(sample)
        for offset, bar in enumerate(sample):
            prior = self.bars[start + offset - 1].close if start + offset > 0 else bar.close
            true_ranges.append(max(bar.high - bar.low, abs(bar.high - prior), abs(bar.low - prior)))
        return sum(true_ranges, Decimal("0")) / Decimal(len(true_ranges))

    @staticmethod
    def _validate_bar(bar):
        if bar.time.tzinfo is None:
            raise ValueError("Timezone-aware bars required")
        values = (bar.open, bar.high, bar.low, bar.close)
        if not all(isinstance(value, Decimal) and value.is_finite() and value > 0 for value in values):
            raise ValueError("OHLC must be positive finite Decimal values")
        if not bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high:
            raise ValueError("Invalid OHLC")
