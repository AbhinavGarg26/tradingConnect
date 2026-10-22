"""One closed-candle decision engine for NIFTY breakout trading.

Prices, stops and targets are NIFTY index points, not option premiums.
The caller owns option selection, order fills and broker-side protection.
"""

from dataclasses import dataclass
from datetime import datetime, time as clock_time
from decimal import Decimal
from typing import Optional


STRATEGY_VERSION = "v3"


@dataclass(frozen=True)
class Bar:
    time: object
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    def __post_init__(self):
        if self.low > self.high or not self.low <= self.open <= self.high or not self.low <= self.close <= self.high:
            raise ValueError("Invalid OHLC bar")


@dataclass(frozen=True)
class Decision:
    action: str  # BUY_CE, BUY_PE, EXIT_CE, EXIT_PE, HOLD
    time: object
    index_price: Decimal
    stop: Optional[Decimal] = None
    target: Optional[Decimal] = None
    reason: str = ""
    reference_high: Optional[Decimal] = None
    reference_low: Optional[Decimal] = None
    prior_entry_high: Optional[Decimal] = None
    prior_entry_low: Optional[Decimal] = None
    candidate_side: Optional[str] = None
    proposed_risk_points: Optional[Decimal] = None


class Nifty15mBreakout:
    """Evaluate only completed, ordered 15-minute NIFTY index bars.

    Reference range is the immediately preceding completed bar. Entry is
    signalled at the breakout bar close. Stops/targets are checked on later
    bar closes, avoiding an unknowable intrabar fill ordering. After the first
    trade, another entry must also close outside the prior entry candle's
    range. From the second trade onward, favorable 10-point closes ratchet
    the stop to lock in that many index points from the signal close.
    """

    def __init__(self, stop_buffer=5, target_points=30, max_risk_points=30,
                 max_trades_per_day=4):
        self.stop_buffer = Decimal(str(stop_buffer))
        self.target_points = Decimal(str(target_points))
        self.max_risk_points = (Decimal(str(max_risk_points))
                                if max_risk_points is not None else None)
        if self.stop_buffer < 0 or self.target_points <= 0:
            raise ValueError("Invalid stop buffer or target")
        if self.max_risk_points is not None and self.max_risk_points <= 0:
            raise ValueError("Maximum risk must be positive")
        if max_trades_per_day < 1:
            raise ValueError("Maximum trades per day must be positive")
        self.max_trades_per_day = max_trades_per_day
        self.previous: Optional[Bar] = None
        self.position: Optional[str] = None
        self.stop: Optional[Decimal] = None
        self.target: Optional[Decimal] = None
        self.entry_price: Optional[Decimal] = None
        self.entry_count = 0
        self.last_entry_bar: Optional[Bar] = None

    def on_bar(self, bar: Bar) -> Decision:
        if self.previous and bar.time <= self.previous.time:
            raise ValueError("Bars must be unique and chronological")
        if (self.previous and isinstance(bar.time, datetime)
                and isinstance(self.previous.time, datetime)
                and bar.time.date() != self.previous.time.date()):
            self.previous = None
            self.entry_count = 0
            self.last_entry_bar = None
        reference_bar = self.previous
        prior_entry_bar = self.last_entry_bar
        action, reason = "HOLD", ""
        candidate_side, proposed_risk_points = None, None
        stop, target = self.stop, self.target
        session_end = isinstance(bar.time, datetime) and bar.time.time() >= clock_time(15, 0)
        if self.position is not None:
            if self.position == "CE":
                hit_stop, hit_target = bar.close <= self.stop, bar.close >= self.target
            else:
                hit_stop, hit_target = bar.close >= self.stop, bar.close <= self.target
            if session_end or hit_stop or hit_target:
                action = f"EXIT_{self.position}"
                reason = "session_end" if session_end else ("stop" if hit_stop else "target")
            elif self.entry_count >= 2:
                direction = Decimal(1) if self.position == "CE" else Decimal(-1)
                favorable = direction * (bar.close - self.entry_price)
                steps = favorable // Decimal(10)
                if steps >= 1:
                    locked_stop = self.entry_price + direction * steps * Decimal(10)
                    old_stop = self.stop
                    self.stop = (max(self.stop, locked_stop) if self.position == "CE"
                                 else min(self.stop, locked_stop))
                    stop = self.stop
                    if self.stop != old_stop:
                        reason = "trail_stop"
        elif self.position is None and self.previous is not None and not session_end:
            if (bar.close > self.previous.high
                    and (self.last_entry_bar is None or bar.close > self.last_entry_bar.high)):
                action, stop, target = "BUY_CE", self.previous.low - self.stop_buffer, bar.close + self.target_points
                reason = ("first_break_above_previous_high" if self.entry_count == 0
                          else "fresh_break_above_prior_entry_high")
            elif (bar.close < self.previous.low
                    and (self.last_entry_bar is None or bar.close < self.last_entry_bar.low)):
                action, stop, target = "BUY_PE", self.previous.high + self.stop_buffer, bar.close - self.target_points
                reason = ("first_break_below_previous_low" if self.entry_count == 0
                          else "fresh_break_below_prior_entry_low")
            if action.startswith("BUY") and self.entry_count >= self.max_trades_per_day:
                action, reason = "HOLD", "trade_limit"
            if action.startswith("BUY") and self.max_risk_points is not None:
                risk = bar.close - stop if action == "BUY_CE" else stop - bar.close
                if risk > self.max_risk_points:
                    candidate_side, proposed_risk_points = action[-2:], risk
                    action, reason = "HOLD", "risk_cap"

        if action.startswith("BUY"):
            self.position, self.stop, self.target = action[-2:], stop, target
            self.entry_price = bar.close
            self.entry_count += 1
            self.last_entry_bar = bar
        elif action.startswith("EXIT"):
            self.position = self.stop = self.target = self.entry_price = None
        self.previous = bar
        return Decision(
            action, bar.time, bar.close, stop, target, reason,
            reference_high=reference_bar.high if action.startswith("BUY") and reference_bar else None,
            reference_low=reference_bar.low if action.startswith("BUY") and reference_bar else None,
            prior_entry_high=prior_entry_bar.high if action.startswith("BUY") and prior_entry_bar else None,
            prior_entry_low=prior_entry_bar.low if action.startswith("BUY") and prior_entry_bar else None,
            candidate_side=candidate_side,
            proposed_risk_points=proposed_risk_points,
        )


def bar_from_candle(candle) -> Bar:
    """Adapter for trading.candle_builder.Candle (also usable in live callbacks)."""
    if candle.timeframe_minutes != 15 or not candle.is_closed:
        raise ValueError("Expected a closed 15-minute candle")
    return Bar(candle.open_time, *(Decimal(str(getattr(candle, key))) for key in ("open", "high", "low", "close")))


class Nifty15mLiveAdapter:
    """Accept closed WebSocket candles and forward actionable decisions."""

    def __init__(self, nifty_token: int, execute, *, max_risk_points=30):
        self.nifty_token = nifty_token
        self.execute = execute
        self.strategy = Nifty15mBreakout(max_risk_points=max_risk_points)

    def on_candle_close(self, candle, _user_id=None):
        if candle.instrument_token != self.nifty_token or candle.timeframe_minutes != 15:
            return None
        decision = self.strategy.on_bar(bar_from_candle(candle))
        if decision.action != "HOLD":
            self.execute(decision)
        return decision
