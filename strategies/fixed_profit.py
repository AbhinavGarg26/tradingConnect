"""Fixed Profit v2: opening-range support/resistance confirmation, index risk."""
from datetime import time, timedelta
from decimal import Decimal

from strategies.nifty_opening_range import IST, NiftyOpeningRange, OpeningSignal

STRATEGY_NAME = 'fixed_profit'
STRATEGY_VERSION = 'v2-sl15-fixed-entry'


class FixedProfit(NiftyOpeningRange):
    """Completed-bar setup engine. Caller confirms next-quote fills explicitly.

    retest: break opening high/low, later retest from outside, then continuation.
    rejection: opening low holds as support / high as resistance from inside.
    Default 30 index target / 15 index stop, one confirmed entry per session.
    Entry must be within 10 points of the level and its fixed stop must sit
    at least one point beyond the rejection candle's adverse extreme using the
    original 10-point entry qualification. Actual exits use the 15-point stop.
    """

    def __init__(self, *, min_range_points=40, confirmation_minutes=5,
                 tolerance_points=2, setup_mode='retest', follow_through=False, stop_points=15, entry_stop_points=10):
        if confirmation_minutes not in {1, 3, 5}:
            raise ValueError('Confirmation candles must be 1, 3, or 5 minutes')
        if setup_mode not in {'retest', 'rejection'}:
            raise ValueError('Choose retest or rejection setup')
        self.min_range_points = Decimal(str(min_range_points))
        self.tolerance = Decimal(str(tolerance_points))
        if not all(v.is_finite() and v >= 0 for v in (self.min_range_points, self.tolerance)):
            raise ValueError('Range threshold and tolerance must be finite and nonnegative')
        super().__init__(risk_mode='index', reference_minutes=30, max_entries=1,
                         stop_points=stop_points, target_points=30)
        self.entry_stop_points = Decimal(str(entry_stop_points))
        if not self.entry_stop_points.is_finite() or self.entry_stop_points <= 0:
            raise ValueError('Entry stop distance must be positive and finite')
        self.confirmation_minutes = confirmation_minutes
        self.setup_mode = setup_mode
        self.follow_through = follow_through
        self.eligible = False
        self.breakout = self.rejection = self.entry_setup = None
        self.last_bar_end = None

    def set_reference(self, bar, *, observed_at):
        self._validate_bar(bar)
        super().set_reference(bar, observed_at=observed_at)
        self.eligible = bar.high - bar.low > self.min_range_points
        self.breakout = self.rejection = self.entry_setup = None
        self.last_bar_end = bar.time + timedelta(minutes=30)

    @staticmethod
    def _validate_bar(bar):
        values = (bar.open, bar.high, bar.low, bar.close)
        if not all(v.is_finite() and v > 0 for v in values):
            raise ValueError('OHLC must be positive finite Decimal values')
        if not bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high:
            raise ValueError('Invalid OHLC')
        if bar.time.tzinfo is None:
            raise ValueError('Timezone-aware candles required')

    def _entry_valid(self, price, setup):
        side, level, adverse = setup
        return (abs(price - level) <= 10 and
                side * (price - level) > 0 and
                side * (price - side * self.entry_stop_points - adverse) <= -1)

    def on_bar(self, bar, *, observed_at):
        """bar.time is open time; use only after its full duration has closed."""
        self._validate_bar(bar)
        if self.reference is None:
            raise ValueError('Set completed opening reference first')
        end = bar.time + timedelta(minutes=self.confirmation_minutes)
        if observed_at.tzinfo is None or observed_at < end:
            raise ValueError('Confirmation candle must be closed')
        start = bar.time.astimezone(IST)
        if start.date() != self.reference.time.astimezone(IST).date():
            raise ValueError('Set reference for the current session')
        if bar.time < self.last_bar_end:
            raise ValueError('Candles must be chronological and non-overlapping')
        if observed_at.astimezone(IST).date() != start.date():
            raise ValueError('Observation must belong to candle session')
        if bar.time > self.last_bar_end:
            self.breakout = self.rejection = None
        self.last_bar_end = end
        if (self.position or self.pending or self.entries >= self.max_entries or
                not self.eligible or end.astimezone(IST).time() >= time(14, 30)):
            return None
        high, low = self.reference.high, self.reference.low
        # A rejection is confirmed only by a later close past its favorable extreme.
        if self.rejection:
            side, level, adverse, favorable, expiry = self.rejection
            if end > expiry or side * (bar.close - level) <= 0 or side * ((bar.low if side == 1 else bar.high) - adverse) < 0:
                self.rejection = None
            elif side * (bar.close - favorable) > 0:
                return self._signal(end, bar.close, (side, level, adverse))
            else:
                return None
        if self.setup_mode == 'retest':
            if self.breakout:
                side, level, expiry = self.breakout
                if end > expiry or side * (bar.close - level) <= 0:
                    self.breakout = None
                else:
                    adverse = bar.low if side == 1 else bar.high
                    touches = abs(adverse - level) <= self.tolerance
                    directional = side * (bar.close - bar.open) > 0
                    if touches and directional:
                        return self._reject_or_signal(bar, end, side, level)
                    return None
            # A breakout candle cannot also serve as its own retest.
            side = 1 if bar.close > high + 2 else -1 if bar.close < low - 2 else 0
            if side:
                self.breakout = (side, high if side == 1 else low, end + timedelta(minutes=30))
        else:
            # Original opening low is support; original high is resistance.
            if abs(bar.low - low) <= self.tolerance and low < bar.close < high and bar.close > bar.open:
                return self._reject_or_signal(bar, end, 1, low)
            if abs(bar.high - high) <= self.tolerance and low < bar.close < high and bar.close < bar.open:
                return self._reject_or_signal(bar, end, -1, high)
        return None

    def _reject_or_signal(self, bar, end, side, level):
        adverse = bar.low if side == 1 else bar.high
        if not self.follow_through:
            return self._signal(end, bar.close, (side, level, adverse))
        self.rejection = (side, level, adverse, bar.high if side == 1 else bar.low,
                          end + timedelta(minutes=15))
        return None

    def _signal(self, timestamp, price, setup):
        self.breakout = self.rejection = None
        if not self._entry_valid(price, setup):
            return None
        self.entry_setup = setup
        self.pending = OpeningSignal('BUY_CE' if setup[0] == 1 else 'BUY_PE',
                                     timestamp, price, 'fixed_profit_' + self.setup_mode)
        return self.pending

    def confirm_entry(self, *, index_price, option_fill=1):
        price = Decimal(str(index_price))
        if not self.entry_setup or not price.is_finite() or not self._entry_valid(price, self.entry_setup):
            raise ValueError('Fill would chase level or put stop inside rejection wick')
        super().confirm_entry(index_price=price, option_fill=option_fill)
        self.entry_setup = None

    def on_tick(self, timestamp, index_price, *, option_price=None):
        """Tick exit management only; entries require on_bar confirmation."""
        signal = super().on_tick(timestamp, index_price, option_price=option_price)
        if signal and signal.action.startswith('BUY_'):
            self.pending = None
            return None
        return signal

    def reject_signal(self):
        super().reject_signal()
        self.breakout = self.rejection = self.entry_setup = None
