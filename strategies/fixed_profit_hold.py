"""Fixed Profit: validated opening 30-minute breakout with fixed exits."""
from datetime import timedelta
from decimal import Decimal

from strategies.nifty_opening_range import NiftyOpeningRange, OpeningSignal

STRATEGY_NAME = 'fixed_profit'
STRATEGY_VERSION = 'v1'


class FixedProfit(NiftyOpeningRange):
    """Require explicit range, validation duration and points basis.

    Validation means remaining beyond the opening high/low on every observed
    quote for the specified duration. A return inside or direction reversal
    resets the timer. Callers must provide a continuous quote stream.
    Inherited fill confirmation and rejection methods manage order lifecycle.
    """

    def __init__(self, *, min_range_points, validation_minutes, risk_mode="index"):
        threshold = Decimal(str(min_range_points))
        duration = Decimal(str(validation_minutes))
        if not threshold.is_finite() or threshold <= 0:
            raise ValueError('Range threshold must be positive and finite')
        if not duration.is_finite() or duration <= 0:
            raise ValueError('Validation minutes must be positive and finite')
        if risk_mode not in {'index', 'premium'}:
            raise ValueError('Choose index or premium points')
        super().__init__(reference_minutes=30, max_entries=1,
                         stop_points=10, target_points=30, risk_mode=risk_mode)
        self.min_range_points = threshold
        self.validation_duration = timedelta(minutes=float(duration))
        self._candidate = self._candidate_since = None
        self.eligible = False

    def set_reference(self, bar, *, observed_at):
        prices = [Decimal(str(value)) for value in
                  (bar.open, bar.high, bar.low, bar.close)]
        if not all(value.is_finite() and value > 0 for value in prices):
            raise ValueError('Opening prices must be positive and finite')
        opening, high, low, close = prices
        if not low <= min(opening, close) <= max(opening, close) <= high:
            raise ValueError('Invalid opening candle OHLC')
        super().set_reference(bar, observed_at=observed_at)
        self.eligible = high - low > self.min_range_points
        self._candidate = self._candidate_since = None

    def on_tick(self, timestamp, index_price, *, option_price=None):
        # Delegate chronology, session checks, and all fixed exit handling.
        signal = super().on_tick(timestamp, index_price, option_price=option_price)
        if signal is None or not signal.action.startswith('BUY_'):
            if not self.position and not self.pending:
                self._candidate = self._candidate_since = None
            return signal
        self.pending = None
        if not self.eligible:
            self._candidate = self._candidate_since = None
            return None
        if self._candidate != signal.action:
            self._candidate = signal.action
            self._candidate_since = timestamp
            return None
        if timestamp - self._candidate_since < self.validation_duration:
            return None
        self.pending = OpeningSignal(signal.action, timestamp, signal.index_price,
                                     'fixed_profit_validated_breakout')
        self._candidate = self._candidate_since = None
        return self.pending

    def reject_signal(self):
        super().reject_signal()
        self._candidate = self._candidate_since = None
