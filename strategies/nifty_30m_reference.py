"""Nifty 30m body-reference strategy; completed 1m signals, no broker orders."""
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

IST = ZoneInfo('Asia/Kolkata')
STRATEGY_NAME = 'nifty_30m_reference'
STRATEGY_VERSION = 'v3'


@dataclass(frozen=True)
class ReferenceSignal:
    action: str
    time: datetime
    index_price: Decimal
    reason: str
    ce_reference: Decimal
    pe_reference: Decimal
    stop: Decimal | None = None


class Nifty30mReference:
    """Aggregate complete 30m buckets from consecutive 1m interval-start bars.

    First entry: first post-opening close outside the initial body band.
    Later entries: fresh crossing of the applicable retained reference.
    Only matching-color >40-point bodies update the held side, never loosening.
    Confirm every fill; counts and stop activate on confirmed buy, not signal.
    """
    def __init__(self, *, stop_points=20, max_entries=3, buffer_percent=5):
        self.stop_points = Decimal(str(stop_points))
        self.buffer_percent = Decimal(str(buffer_percent))
        if not self.stop_points.is_finite() or self.stop_points <= 0:
            raise ValueError('Stop points must be positive and finite')
        if not self.buffer_percent.is_finite() or not 0 <= self.buffer_percent <= 100:
            raise ValueError('Buffer percent must be between 0 and 100')
        if not isinstance(max_entries, int) or isinstance(max_entries, bool) or max_entries < 1:
            raise ValueError('Maximum entries must be a positive integer')
        self.max_entries = max_entries
        self.session = self.last_time = self.last_close = None
        self.position = self.pending = self.stop = None
        self.ce_reference = self.pe_reference = None
        self.count = self.entry_count = 0
        self.bucket_open = None
        self.opening_high = self.opening_low = None
        self.opening_body = self.opening_range = None
        self.opening_eligible = False
        self.reference_updates = []

    def on_bar(self, bar):
        if not isinstance(bar.time, datetime) or bar.time.tzinfo is None:
            raise ValueError('Timezone-aware bar timestamp required')
        start = bar.time.astimezone(IST)
        if start.second or start.microsecond or not time(9, 15) <= start.time() < time(15, 30):
            raise ValueError('Expected NSE one-minute interval start')
        if self.last_time is not None and start <= self.last_time:
            raise ValueError('Candles must be unique and chronological')
        if not all(v.is_finite() and v > 0 for v in (bar.open, bar.high, bar.low, bar.close)):
            raise ValueError('Positive finite Decimal prices required')
        if self.session != start.date():
            if self.position or self.pending:
                raise ValueError('Reconcile previous session before resetting')
            if start.time() != time(9, 15):
                raise ValueError('Session must begin at 09:15')
            self.session = start.date()
            self.count = self.entry_count = 0
            self.last_close = None
            self.ce_reference = self.pe_reference = self.stop = None
            self.reference_updates = []
            self.opening_high = self.opening_low = None
            self.opening_body = self.opening_range = None
            self.opening_eligible = False
        elif start - self.last_time != timedelta(minutes=1):
            raise ValueError('Missing minute; backfill before continuing')
        previous_close = self.last_close
        self.last_time, self.last_close = start, bar.close
        if self.count % 30 == 0:
            self.bucket_open = bar.open
        self.count += 1
        if self.count <= 30:
            self.opening_high = bar.high if self.opening_high is None else max(self.opening_high, bar.high)
            self.opening_low = bar.low if self.opening_low is None else min(self.opening_low, bar.low)
        end = start + timedelta(minutes=1)
        if self.count == 30:
            self.opening_body = abs(self.bucket_open - bar.close)
            self.opening_range = self.opening_high - self.opening_low
            self.opening_eligible = self.opening_body >= 20
            midpoint = (self.bucket_open + bar.close) / 2
            buffer = abs(self.bucket_open - bar.close) * self.buffer_percent / 100
            self.ce_reference, self.pe_reference = midpoint + buffer, midpoint - buffer
        elif self.count % 30 == 0:
            self._update_reference(self.bucket_open, bar.close, end)
        if self.count <= 30 or self.pending:
            return None
        action = reason = None
        if self.position:
            direction = 1 if self.position == 'CE' else -1
            self._manage_stop(bar)
            reference = self.ce_reference if direction == 1 else self.pe_reference
            if end.time() >= time(15, 15):
                reason = 'session_end'
            elif direction * (bar.close - self.stop) <= 0:
                reason = self._stop_exit_reason()
            elif direction * (bar.close - self._reference_exit_level(direction, reference)) <= 0:
                reason = 'reference_cross'
            if reason:
                action = f'EXIT_{self.position}'
        elif (self.opening_eligible and end.time() < time(15, 15)
              and self.entry_count < self.max_entries):
            first = self.entry_count == 0
            ce = bar.close > self.ce_reference and (first or previous_close <= self.ce_reference)
            pe = bar.close < self.pe_reference and (first or previous_close >= self.pe_reference)
            side = 'CE' if ce else 'PE' if pe else None
            if side and self._entry_allowed(side, bar):
                action, reason = f'BUY_{side}', 'opening_break' if first else 'reference_reentry'
        if action:
            self.pending = ReferenceSignal(action, end, bar.close, reason,
                                           self.ce_reference, self.pe_reference, self.stop)
            return self.pending
        return None

    def _reference_exit_level(self, direction, reference):
        """Frozen V3 uses the unbuffered reference for exits."""
        return reference

    def _manage_stop(self, bar):
        """Frozen V3 leaves its initial stop fixed."""
        return None

    def _stop_exit_reason(self):
        return 'fixed_stop'

    def _entry_allowed(self, side, bar):
        """Frozen V3 has no signal-candle color filter."""
        return True

    def _update_reference(self, opening, close, timestamp):
        body = abs(close - opening)
        matching = self.position == 'PE' and close < opening or self.position == 'CE' and close > opening
        if not matching or body <= 40:
            return
        midpoint = (opening + close) / 2
        if self.position == 'PE':
            candidate = opening + 5 if body <= 80 else midpoint + 10
            old = self.pe_reference
            self.pe_reference = min(old, candidate)
            new = self.pe_reference
        else:
            candidate = opening - 5 if body <= 80 else midpoint - 10
            old = self.ce_reference
            self.ce_reference = max(old, candidate)
            new = self.ce_reference
        if new != old:
            self.reference_updates.append(dict(time=timestamp, side=self.position,
                                              body=body, old=old, new=new))

    def confirm_fill(self, *, index_price=None):
        if self.pending is None:
            raise ValueError('No pending signal')
        if self.pending.action.startswith('BUY_'):
            if index_price is None:
                raise ValueError('Entry index price required')
            price = Decimal(str(index_price))
            if not price.is_finite() or price <= 0:
                raise ValueError('Positive finite entry index price required')
            self.position = self.pending.action[-2:]
            direction = 1 if self.position == 'CE' else -1
            self.stop = price - direction * self.stop_points
            self.entry_count += 1
        else:
            self.position = self.stop = None
        self.pending = None

    def reject_signal(self):
        """Only after confirming no fill; reconcile uncertain orders separately."""
        self.pending = None
