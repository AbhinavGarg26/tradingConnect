"""Research-only completed-candle body-midpoint trail; frozen V3 is unchanged."""
from collections import deque
from datetime import timedelta
from decimal import Decimal

from strategies.nifty_30m_reference import IST, Nifty30mReference


class Nifty30mReferenceCandleTrail(Nifty30mReference):
    def __init__(self, *, candle_minutes=30, body_multiple=1.5, lookback=3, **kwargs):
        if candle_minutes not in (15, 30):
            raise ValueError('Candle trail timeframe must be 15 or 30 minutes')
        multiple = Decimal(str(body_multiple))
        if not multiple.is_finite() or multiple < 1:
            raise ValueError('Candle body multiple must be finite and at least 1')
        if not isinstance(lookback, int) or isinstance(lookback, bool) or lookback < 1:
            raise ValueError('Candle trail lookback must be a positive integer')
        self.candle_minutes, self.body_multiple, self.lookback = candle_minutes, multiple, lookback
        self.bodies = deque(maxlen=lookback)
        self.entry_price = self.entry_at = None
        self.trailing_active = False
        self.candle_open = self.candle_start = None
        self.trail_updates = []
        super().__init__(**kwargs)

    def on_bar(self, bar):
        previous_session = self.session
        signal = super().on_bar(bar)  # Validate data; evaluate existing stop/reference first.
        if self.session != previous_session:
            self.bodies.clear()
            self.trail_updates = []
            self.trailing_active = False
        if (self.count - 1) % self.candle_minutes == 0:
            self.candle_open = bar.open
            self.candle_start = bar.time.astimezone(IST)
        if self.count % self.candle_minutes:
            return signal
        body = abs(bar.close - self.candle_open)
        if (self.position and not self.pending and self.entry_at <= self.candle_start
                and len(self.bodies) == self.lookback and body > 0):
            direction = 1 if self.position == 'CE' else -1
            typical_body = sum(self.bodies, Decimal(0)) / self.lookback
            midpoint = (self.candle_open + bar.close) / 2
            if (direction * (bar.close - self.candle_open) > 0
                    and body >= typical_body * self.body_multiple
                    and direction * (midpoint - self.entry_price) > 0
                    and direction * (midpoint - self.stop) > 0):
                old = self.stop
                self.stop = midpoint
                self.trailing_active = True
                self.trail_updates.append(dict(time=bar.time + timedelta(minutes=1),
                    side=self.position, body=body, typical_body=typical_body, old=old, new=midpoint))
        # Compare with prior bodies only: never include the candidate itself.
        self.bodies.append(body)
        return signal

    def _stop_exit_reason(self):
        return 'candle_trail_stop' if self.trailing_active else 'fixed_stop'

    def confirm_fill(self, *, index_price=None):
        entering = self.pending is not None and self.pending.action.startswith('BUY_')
        entry_at = self.pending.time if entering else None
        super().confirm_fill(index_price=index_price)
        self.entry_price = Decimal(str(index_price)) if entering else None
        self.entry_at = entry_at
        self.trailing_active = False
