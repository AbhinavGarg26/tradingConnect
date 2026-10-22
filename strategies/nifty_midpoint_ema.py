"""First-30-minute body midpoint with 1-minute EMA12/36 conditions."""
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

IST = ZoneInfo('Asia/Kolkata')
STRATEGY_NAME = 'nifty_midpoint_ema'
STRATEGY_VERSION = 'v1c10'


def variant_label(ema_gap_points, cooldown_minutes, logic='gap', candle_stop=True, entry_distance_points=3, pe_doji_body_percent=10, early_doji_stop_points=None):
    if logic == 'price_cross':
        if early_doji_stop_points is not None:
            return 'v1c9 (Version C9)'
        if pe_doji_body_percent is not None:
            return 'v1c10 (Version C10)'
        if Decimal(str(entry_distance_points)) != 0:
            return 'v1c6 (Version C6)' if Decimal(str(entry_distance_points)) == 3 and candle_stop else 'custom price_cross'
        return 'v1c3 (Version C3)' if candle_stop else 'v1c2 (Version C2)'
    gap = Decimal(str(ema_gap_points))
    if gap == Decimal('2.5') and cooldown_minutes == 0:
        return 'v1b (Version B)'
    if gap == 0 and cooldown_minutes == 5:
        return 'v1a (Version A)'
    if gap == 0 and cooldown_minutes == 0:
        return 'v1 (Baseline)'
    return 'custom'


@dataclass(frozen=True)
class Signal:
    action: str
    time: datetime
    index_price: Decimal
    reason: str
    upper: Decimal
    lower: Decimal
    ema12: Decimal
    ema36: Decimal
    stop: Decimal | None = None


class NiftyMidpointEMA:
    """Feed completed 1m Bar objects (timestamps are interval starts).

    Daily EMAs seed at the first close, alpha=2/(period+1), with 36 closes
    required before entries. No fixed stop/target. One position; no entry cap.
    Pending actions require confirmed fill or rejection from the caller.
    Exit first; a reversal may enter on a subsequent completed minute.
    """
    def __init__(self, *, buffer_percent=5, square_off=time(15, 15), cooldown_minutes=0,
                 ema_gap_points=Decimal('2.5'), logic='price_cross', candle_stop=True, entry_distance_points=3, max_entries=7, pe_doji_body_percent=10, early_doji_stop_points=None):
        if max_entries is not None and (not isinstance(max_entries, int) or isinstance(max_entries, bool) or max_entries < 1):
            raise ValueError('Maximum entries must be a positive integer or None')
        self.pe_doji_body_percent = None if pe_doji_body_percent is None else Decimal(str(pe_doji_body_percent))
        if self.pe_doji_body_percent is not None and (not self.pe_doji_body_percent.is_finite() or not 0 <= self.pe_doji_body_percent <= 100):
            raise ValueError('PE doji body percent must be finite and between 0 and 100')
        self.early_doji_stop_points = None if early_doji_stop_points is None else Decimal(str(early_doji_stop_points))
        if self.early_doji_stop_points is not None and (not self.early_doji_stop_points.is_finite() or self.early_doji_stop_points <= 0):
            raise ValueError('Early doji stop points must be positive and finite')
        self.entry_index_price = self.entry_fill_time = None
        self.post_entry_candles = 0
        self.doji_stop_tightened = False
        self.max_entries = max_entries
        self.entry_count = 0
        if logic not in {'gap', 'price_cross'}:
            raise ValueError('Unknown strategy logic')
        if logic == 'price_cross' and cooldown_minutes:
            raise ValueError('Version C does not use cooldown')
        self.entry_distance_points = Decimal(str(entry_distance_points))
        if not self.entry_distance_points.is_finite() or self.entry_distance_points < 0:
            raise ValueError('Entry distance must be finite and nonnegative')
        self.candle_stop = candle_stop
        self.stop = None
        self.last_open = None
        self.cross_side = self.cross_stop = None
        self.logic = logic
        self.last_close = None
        self.exit_close_count = 0
        self.exit_armed = False
        self.ema_gap_points = Decimal(str(ema_gap_points))
        if not self.ema_gap_points.is_finite() or self.ema_gap_points < 0:
            raise ValueError('EMA gap must be finite and nonnegative')
        if not isinstance(cooldown_minutes, int) or isinstance(cooldown_minutes, bool) or cooldown_minutes < 0:
            raise ValueError('Cooldown must be a nonnegative integer number of minutes')
        self.cooldown_minutes = cooldown_minutes
        self.cooldown_until = None
        self.buffer_percent = Decimal(str(buffer_percent))
        if not self.buffer_percent.is_finite() or not 0 <= self.buffer_percent <= 100:
            raise ValueError('Buffer percent must be finite and between 0 and 100')
        if not time(9, 45) < square_off <= time(15, 30):
            raise ValueError('Square-off must be after 09:45 and no later than 15:30')
        self.square_off = square_off
        self.last_time = None
        self.session = None
        self.position = self.pending = None
        self.count = 0
        self.opening_open = self.opening_close = None
        self.upper = self.lower = None
        self.ema12 = self.ema36 = None

    def on_bar(self, bar):
        if not isinstance(bar.time, datetime) or bar.time.tzinfo is None:
            raise ValueError('Timezone-aware candle timestamp required')
        start = bar.time.astimezone(IST)
        if start.second or start.microsecond or not time(9, 15) <= start.time() < time(15, 30):
            raise ValueError('Expected NSE session 1-minute candle start')
        if self.last_time and start <= self.last_time:
            raise ValueError('Candles must be unique and chronological')
        if not all(v.is_finite() and v > 0 for v in (bar.open, bar.high, bar.low, bar.close)):
            raise ValueError('Prices must be positive finite Decimals')
        if self.session != start.date():
            if self.position or self.pending:
                raise ValueError('Reconcile previous session position/pending signal')
            if start.time() != time(9, 15):
                raise ValueError('Session must start at 09:15; replay earlier bars on late startup')
            self.count = 0
            self.entry_index_price = self.entry_fill_time = None
            self.post_entry_candles = 0
            self.doji_stop_tightened = False
            self.entry_count = 0
            self.last_close = None
            self.last_open = None
            self.cross_side = self.cross_stop = None
            self.stop = None
            self.exit_close_count = 0
            self.exit_armed = False
            self.cooldown_until = None
            self.opening_open = bar.open
            self.opening_close = self.upper = self.lower = None
            self.ema12 = self.ema36 = None
            self.session = start.date()
        elif start - self.last_time != timedelta(minutes=1):
            raise ValueError('Missing 1-minute candle; backfill before continuing')
        self.last_time = start
        self.count += 1
        prior_open = self.last_open
        prior_close, prior_ema36 = self.last_close, self.ema36
        self.last_open = bar.open
        self.last_close = bar.close
        for period in (12, 36):
            name = f'ema{period}'
            previous = getattr(self, name)
            setattr(self, name, bar.close if previous is None else
                    previous + Decimal(2) / Decimal(period + 1) * (bar.close - previous))
        end = start + timedelta(minutes=1)
        if start.time() == time(9, 44):
            self.opening_close = bar.close
            midpoint = (self.opening_open + self.opening_close) / 2
            buffer = abs(self.opening_close - self.opening_open) * self.buffer_percent / 100
            self.upper, self.lower = midpoint + buffer, midpoint - buffer
        if self.upper is None or self.pending:
            return None
        if self.logic == 'price_cross':
            return self._price_cross_signal(bar, end, prior_close, prior_ema36, prior_open)
        gap = self.ema12 - self.ema36
        threshold = self.ema_gap_points
        ce = bar.close > self.upper and (gap >= threshold if threshold else gap > 0)
        pe = bar.close < self.lower and (gap <= -threshold if threshold else gap < 0)
        action = reason = None
        if self.position:
            if end.time() >= self.square_off:
                action, reason = f'EXIT_{self.position}', 'session_end'
            else:
                price_ok = bar.close > self.upper if self.position == 'CE' else bar.close < self.lower
                # Hysteresis: hold through the central band; exit only at the opposite edge.
                ema_exit = ((gap <= -threshold if self.position == 'CE' else gap >= threshold)
                            if threshold else (gap <= 0 if self.position == 'CE' else gap >= 0))
                if not price_ok or ema_exit:
                    action = f'EXIT_{self.position}'
                    reason = ('price_and_ema_failed' if not price_ok and ema_exit else
                              'price_failed' if not price_ok else 'ema_failed')
        elif (self.count >= 36 and end.time() < self.square_off
              and (self.max_entries is None or self.entry_count < self.max_entries)
              and (self.cooldown_until is None or end >= self.cooldown_until)):
            side = 'CE' if ce else 'PE' if pe else None
            if side:
                action, reason = f'BUY_{side}', 'conditions_met'
        if action:
            self.pending = Signal(action, end, bar.close, reason, self.upper, self.lower,
                                  self.ema12, self.ema36)
            return self.pending
        return None

    def _price_cross_signal(self, bar, end, prior_close, prior_ema36, prior_open):
        action = reason = None
        signal_stop = self.stop
        if self.position:
            if self.entry_fill_time is not None and bar.time >= self.entry_fill_time:
                self.post_entry_candles += 1
                candle_range = bar.high - bar.low
                doji_percent = self.pe_doji_body_percent if self.pe_doji_body_percent is not None else Decimal(10)
                doji = candle_range == 0 or abs(bar.close-bar.open)*100 <= candle_range*doji_percent
                if self.early_doji_stop_points is not None and self.post_entry_candles <= 3 and doji:
                    candidate = self.entry_index_price + (-self.early_doji_stop_points if self.position == 'PE' else self.early_doji_stop_points)
                    improves = self.stop is None or (candidate < self.stop if self.position == 'PE' else candidate > self.stop)
                    if improves:
                        self.stop = candidate
                        self.doji_stop_tightened = True
            signal_stop = self.stop
            stop_hit = (self.stop is not None and
                        (bar.close >= self.stop if self.position == 'PE' else bar.close <= self.stop))
            opposite = bar.close > self.ema12 if self.position == 'PE' else bar.close < self.ema12
            favorable = bar.close < self.ema12 if self.position == 'PE' else bar.close > self.ema12
            if not self.exit_armed:
                self.exit_armed = favorable
                self.exit_close_count = 0
            else:
                self.exit_close_count = self.exit_close_count + 1 if opposite else 0
            if end.time() >= self.square_off:
                action, reason = f'EXIT_{self.position}', 'session_end'
            elif stop_hit:
                action, reason = f'EXIT_{self.position}', ('early_doji_stop' if self.doji_stop_tightened else 'candle_midpoint_stop')
            elif self.exit_close_count >= 2:
                action, reason = f'EXIT_{self.position}', 'two_closes_against_ema12'
        elif (self.count >= 36 and end.time() < self.square_off and prior_close is not None
              and (self.max_entries is None or self.entry_count < self.max_entries)):
            # Remember a fresh crossing until distance and price filters confirm it.
            if ((self.cross_side == 'PE' and bar.close >= self.ema36)
                    or (self.cross_side == 'CE' and bar.close <= self.ema36)):
                self.cross_side = self.cross_stop = None
            crossed_ce = prior_close <= prior_ema36 and bar.close > self.ema36
            crossed_pe = prior_close >= prior_ema36 and bar.close < self.ema36
            if crossed_ce or crossed_pe:
                self.cross_side = 'CE' if crossed_ce else 'PE'
                self.cross_stop = (prior_open + prior_close) / 2 if self.candle_stop else None
            ce = (self.cross_side == 'CE' and bar.close > self.upper
                  and bar.close - self.ema36 >= self.entry_distance_points)
            pe = (self.cross_side == 'PE' and bar.close < self.lower
                  and self.ema36 - bar.close >= self.entry_distance_points)
            if pe and self.pe_doji_body_percent is not None:
                candle_range = bar.high - bar.low
                doji = (candle_range == 0 or
                        abs(bar.close - bar.open) * 100 <= candle_range * self.pe_doji_body_percent)
                pe = not doji
            side = 'CE' if ce else 'PE' if pe else None
            if side:
                action, reason = f'BUY_{side}', 'cross_confirmed_ema36'
                signal_stop = self.cross_stop
        if action:
            self.pending = Signal(action, end, bar.close, reason, self.upper, self.lower,
                                  self.ema12, self.ema36, signal_stop)
            return self.pending
        return None

    def confirm_fill(self, *, fill_time=None, index_price=None):
        if self.pending is None:
            raise ValueError('No pending signal')
        if self.pending.action.startswith('EXIT_') and self.cooldown_minutes:
            if (not isinstance(fill_time, datetime) or fill_time.tzinfo is None
                    or fill_time < self.pending.time):
                raise ValueError('An aware exit fill time at/after the signal is required')
            self.cooldown_until = fill_time + timedelta(minutes=self.cooldown_minutes)
        if self.pending.action.startswith('BUY_'):
            price = self.pending.index_price if index_price is None else Decimal(str(index_price))
            actual_time = self.pending.time if fill_time is None else fill_time
            if not price.is_finite() or price <= 0:
                raise ValueError('Positive finite index fill price required')
            if not isinstance(actual_time, datetime) or actual_time.tzinfo is None or actual_time < self.pending.time:
                raise ValueError('Aware entry fill time at or after signal required')
            self.entry_index_price, self.entry_fill_time = price, actual_time
            self.entry_count += 1
        else:
            self.entry_index_price = self.entry_fill_time = None
        self.post_entry_candles = 0
        self.doji_stop_tightened = False
        self.stop = self.pending.stop if self.pending.action.startswith('BUY_') else None
        self.position = self.pending.action[-2:] if self.pending.action.startswith('BUY_') else None
        self.exit_close_count = 0
        self.exit_armed = False
        self.cross_side = self.cross_stop = None
        self.pending = None

    def reject_signal(self):
        """Only after confirming no fill; uncertain orders require reconciliation."""
        self.cross_side = self.cross_stop = None
        self.pending = None
