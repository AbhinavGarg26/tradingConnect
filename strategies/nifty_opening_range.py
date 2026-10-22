"""Fixed opening-range signals; execution and confirmed fills belong to caller."""
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

STRATEGY_NAME = 'nifty_opening_range_breakout'
STRATEGY_VERSION = 'v1'
IST = ZoneInfo('Asia/Kolkata')


def current_atm_option(instruments, today, side, spot):
    """Nearest listed unexpired NIFTY expiry; nearest strike, lower on ties."""
    choices = [i for i in instruments if i.get('name') == 'NIFTY'
               and i.get('instrument_type') == side and i.get('expiry') is not None
               and i['expiry'] >= today]
    if not choices:
        raise ValueError('No current NIFTY option contract available')
    expiry = min(i['expiry'] for i in choices)
    return min((i for i in choices if i['expiry'] == expiry),
               key=lambda i: (abs(Decimal(str(i['strike'])) - Decimal(str(spot))),
                              Decimal(str(i['strike']))))


@dataclass(frozen=True)
class OpeningSignal:
    action: str
    time: datetime
    index_price: Decimal
    reason: str


class NiftyOpeningRange:
    """09:15 opening range, tick breaches, one entry per day by default.

    Default risk_mode is index: 5-point stop and 30-point target. Premium
    risk uses a confirmed option fill and subsequent option quotes. Index
    risk uses the confirmed entry's index quote. Opposite-range risk uses
    opening low - 5 for CE / opening high + 5 for PE, and 30 index target.
    No orders are submitted by this engine. Confirm entry before feeding ticks.
    """
    def __init__(self, *, risk_mode='index', reference_minutes=15, max_entries=1,
                 stop_points=5, target_points=30):
        if risk_mode not in {'premium', 'index', 'opposite_range'}:
            raise ValueError('Choose premium, index, or opposite_range risk')
        if reference_minutes not in {15, 30} or max_entries < 1:
            raise ValueError('Expected a 15/30 minute range and positive entry limit')
        self.stop_points = Decimal(str(stop_points))
        self.target_points = Decimal(str(target_points))
        if not all(v.is_finite() and v > 0 for v in (self.stop_points, self.target_points)):
            raise ValueError('Stop and target must be positive finite points')
        self.risk_mode = risk_mode
        self.reference_minutes = reference_minutes
        self.max_entries = max_entries
        self.reference = None
        self.last_time = None
        self.position = None
        self.pending = None
        self.entries = 0
        self.stop = self.target = None

    def set_reference(self, bar, *, observed_at):
        if bar.time.tzinfo is None or observed_at.tzinfo is None:
            raise ValueError('Timezone-aware timestamps required')
        start = bar.time.astimezone(IST)
        end = start + timedelta(minutes=self.reference_minutes)
        if start.time() != time(9, 15) or observed_at < end:
            raise ValueError('Reference must be the completed opening candle')
        if observed_at.astimezone(IST).date() != start.date():
            raise ValueError('Reference must belong to the current session')
        if self.position or self.pending:
            raise ValueError('Resolve position or pending signal before resetting')
        if self.reference and start.date() <= self.reference.time.astimezone(IST).date():
            raise ValueError('Opening range can only be set once per session')
        self.reference = bar
        self.last_time = observed_at
        self.entries = 0

    def on_tick(self, timestamp, index_price, *, option_price=None):
        if timestamp.tzinfo is None:
            raise ValueError('Timezone-aware timestamps required')
        now = timestamp.astimezone(IST)
        if self.reference is None:
            return None
        if now.date() != self.reference.time.astimezone(IST).date():
            raise ValueError('Load the new session opening range; reconcile any position')
        if timestamp < self.last_time:
            raise ValueError('Quotes must be chronological')
        self.last_time = timestamp
        spot = Decimal(str(index_price))
        if not spot.is_finite() or spot <= 0:
            raise ValueError('Invalid index price')
        if self.pending:
            return None
        if self.position:
            reason = 'session_end' if now.time() >= time(15, 15) else None
            if not reason:
                if self.risk_mode == 'premium' and option_price is None:
                    raise ValueError('An option quote is required for premium exits')
                price = Decimal(str(option_price)) if self.risk_mode == 'premium' else spot
                if not price.is_finite() or price <= 0:
                    raise ValueError('Invalid exit quote')
                ascending = self.risk_mode == 'premium' or self.position == 'CE'
                if (price <= self.stop if ascending else price >= self.stop):
                    reason = 'stop'
                elif (price >= self.target if ascending else price <= self.target):
                    reason = 'target'
            if reason:
                self.pending = OpeningSignal(f'EXIT_{self.position}', timestamp, spot, reason)
                return self.pending
        elif now.time() < time(15, 15) and self.entries < self.max_entries:
            side = 'CE' if spot > self.reference.high else 'PE' if spot < self.reference.low else None
            if side:
                self.pending = OpeningSignal(f'BUY_{side}', timestamp, spot, 'opening_range_break')
                return self.pending
        return None

    def confirm_entry(self, *, index_price, option_fill):
        if not self.pending or not self.pending.action.startswith('BUY_'):
            raise ValueError('No pending entry')
        spot, premium = Decimal(str(index_price)), Decimal(str(option_fill))
        if not all(v.is_finite() and v > 0 for v in (spot, premium)):
            raise ValueError('Positive confirmed prices required')
        side = self.pending.action[-2:]
        if self.risk_mode == 'premium':
            if premium <= self.stop_points:
                raise ValueError('Option premium must exceed the 5-point stop distance')
            self.stop, self.target = premium - self.stop_points, premium + self.target_points
        else:
            direction = 1 if side == 'CE' else -1
            self.target = spot + direction * self.target_points
            self.stop = (spot - direction * self.stop_points if self.risk_mode == 'index' else
                         self.reference.low - self.stop_points if side == 'CE' else self.reference.high + self.stop_points)
        self.position = side
        self.entries += 1
        self.pending = None

    def confirm_exit(self):
        if not self.pending or not self.pending.action.startswith('EXIT_'):
            raise ValueError('No pending exit')
        self.position = self.pending = self.stop = self.target = None

    def reject_signal(self):
        """Call only after confirming the order did not fill; reconcile uncertainty."""
        self.pending = None
