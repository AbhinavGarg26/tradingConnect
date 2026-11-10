"""State machine for the frozen NIFTY noise-area momentum strategy."""
from dataclasses import dataclass
from datetime import time, timedelta
from decimal import Decimal

KEY = "nifty_noise_area"
VERSION = "noise_area_v1"
LOOKBACK = 14


@dataclass(frozen=True)
class NoiseSignal:
    action: str
    time: object
    index_price: Decimal
    reason: str
    upper: Decimal | None = None
    lower: Decimal | None = None


class NoiseAreaMomentum:
    def __init__(self, history, previous_close, *, multiplier=Decimal("1")):
        self.history = {clock: [Decimal(str(v)) for v in values]
                        for clock, values in history.items()}
        if any(len(values) < LOOKBACK for values in self.history.values()):
            raise ValueError("Every decision time requires fourteen prior sessions")
        self.previous_close = Decimal(str(previous_close))
        self.multiplier = Decimal(str(multiplier))
        if self.previous_close <= 0 or self.multiplier <= 0:
            raise ValueError("Invalid previous close or multiplier")
        self.session_date = self.session_open = None
        self.position = self.pending = None
        self.entries = 0

    @staticmethod
    def decision_clock(stamp):
        end = stamp + timedelta(minutes=1)
        minutes = end.hour * 60 + end.minute
        return end.time().replace(second=0, microsecond=0) if (
            minutes >= 9 * 60 + 45 and minutes < 15 * 60 + 15
            and (minutes - (9 * 60 + 15)) % 30 == 0) else None

    def start_session(self, stamp, opening_price):
        price = Decimal(str(opening_price))
        if stamp.tzinfo is None or price <= 0:
            raise ValueError("Timezone-aware session and positive open required")
        if self.position or self.pending:
            raise ValueError("Cannot reset with unresolved position or signal")
        self.session_date, self.session_open = stamp.date(), price
        self.entries = 0

    def on_minute(self, stamp, close):
        price = Decimal(str(close))
        if self.session_open is None or stamp.date() != self.session_date:
            raise ValueError("Start the current session before feeding minutes")
        if price <= 0 or self.pending:
            return None
        end = stamp + timedelta(minutes=1)
        if end.time() >= time(15, 15):
            if self.position:
                self.pending = NoiseSignal(f"EXIT_{self.position}", end, price, "session_end")
            return self.pending
        clock = self.decision_clock(stamp)
        if clock is None:
            return None
        moves = self.history.get(clock)
        if moves is None or len(moves) < LOOKBACK:
            raise ValueError(f"Missing fourteen-session history for {clock}")
        sigma = sum(moves[-LOOKBACK:], Decimal()) / LOOKBACK
        upper = max(self.session_open, self.previous_close) * (1 + self.multiplier * sigma)
        lower = min(self.session_open, self.previous_close) * (1 - self.multiplier * sigma)
        desired = "CE" if price > upper else "PE" if price < lower else None
        if self.position and desired != self.position:
            self.pending = NoiseSignal(f"EXIT_{self.position}", end, price,
                                       "signal_flip" if desired else "inside_noise_area", upper, lower)
        elif not self.position and desired and end.time() <= time(14, 45):
            self.pending = NoiseSignal(f"BUY_{desired}", end, price,
                                       "noise_area_break", upper, lower)
        return self.pending

    def confirm_entry(self):
        if not self.pending or not self.pending.action.startswith("BUY_"):
            raise ValueError("No pending entry")
        self.position = self.pending.action[-2:]
        self.entries += 1
        self.pending = None

    def confirm_exit(self):
        if not self.pending or not self.pending.action.startswith("EXIT_"):
            raise ValueError("No pending exit")
        self.position = self.pending = None

    def reject(self):
        self.pending = None
