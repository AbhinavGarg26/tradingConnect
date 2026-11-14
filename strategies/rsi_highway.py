"""Deterministic, close-confirmed RSI Highway swing strategy.

The engine is deliberately broker-agnostic.  It consumes completed monthly,
weekly and daily candles and returns a decision plus the next durable state.
Orders, sizing, universe selection and breadth calculation belong to callers.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from enum import Enum
from math import isfinite
from typing import Sequence


KEY = "rsi_highway"
VERSION = "v2-rising-weekly-confirmation"


@dataclass(frozen=True)
class Candle:
    day: date
    open: float
    high: float
    low: float
    close: float

    def __post_init__(self):
        values = (self.open, self.high, self.low, self.close)
        if not all(isfinite(value) and value > 0 for value in values):
            raise ValueError("Candle prices must be finite and positive")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("Candle high/low does not contain its body")


@dataclass(frozen=True)
class Config:
    rsi_period: int = 14
    trend_rsi: float = 60.0
    entry_rsi: float = 50.0
    confirmation_rsi: float = 52.0
    bounce_ceiling: float = 52.0
    require_weekly_rsi_rising: bool = True
    max_entries: int = 3
    swing_lookback: int = 5


@dataclass(frozen=True)
class State:
    position_open: bool = False
    entries: int = 0
    stop: float | None = None
    last_entry_day: date | None = None


class Action(str, Enum):
    ENTER = "ENTER"
    ADD = "ADD"
    HOLD = "HOLD"
    EXIT = "EXIT"
    NONE = "NONE"


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str
    monthly_rsi: float | None
    weekly_rsi: float | None
    daily_rsi: float | None
    pattern: str | None = None
    stop: float | None = None


def rsi(closes: Sequence[float], period: int = 14) -> list[float | None]:
    """Return Wilder RSI values aligned with ``closes``."""
    if period < 2:
        raise ValueError("RSI period must be at least 2")
    if any(not isfinite(float(value)) or float(value) <= 0 for value in closes):
        raise ValueError("RSI closes must be finite and positive")
    result: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return result
    changes = [float(closes[i]) - float(closes[i - 1]) for i in range(1, len(closes))]
    avg_gain = sum(max(change, 0.0) for change in changes[:period]) / period
    avg_loss = sum(max(-change, 0.0) for change in changes[:period]) / period

    def value(gain: float, loss: float) -> float:
        if loss == 0:
            return 100.0 if gain > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + gain / loss)

    result[period] = value(avg_gain, avg_loss)
    for index in range(period + 1, len(closes)):
        change = changes[index - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
        result[index] = value(avg_gain, avg_loss)
    return result


def bullish_pattern(previous: Candle, current: Candle) -> str | None:
    current_body = abs(current.close - current.open)
    previous_body = abs(previous.close - previous.open)
    engulfing = (
        previous.close < previous.open
        and current.close > current.open
        and current.open <= previous.close
        and current.close >= previous.open
        and current_body >= previous_body
    )
    if engulfing:
        return "bullish_engulfing"
    lower_wick = min(current.open, current.close) - current.low
    upper_wick = current.high - max(current.open, current.close)
    if current.close > current.open and current_body > 0 and lower_wick >= 2 * current_body and upper_wick <= current_body:
        return "hammer"
    if current.close > current.open and current.close > previous.high:
        return "breakout"
    return None


def _last_rsi(candles: Sequence[Candle], period: int) -> float | None:
    values = rsi([candle.close for candle in candles], period)
    return values[-1] if values else None


def evaluate(
    monthly: Sequence[Candle],
    weekly: Sequence[Candle],
    daily: Sequence[Candle],
    *,
    breadth_healthy: bool,
    state: State = State(),
    config: Config = Config(),
) -> tuple[Decision, State]:
    """Evaluate the latest completed daily candle and return decision/state."""
    if len(daily) < max(config.rsi_period + 2, config.swing_lookback):
        return Decision(Action.NONE, "insufficient_daily_history", None, None, None), state
    monthly_rsi = _last_rsi(monthly, config.rsi_period)
    weekly_values = rsi([candle.close for candle in weekly], config.rsi_period)
    weekly_rsi = weekly_values[-1] if weekly_values else None
    previous_weekly_rsi = weekly_values[-2] if len(weekly_values) > 1 else None
    daily_values = rsi([candle.close for candle in daily], config.rsi_period)
    previous_rsi, daily_rsi = daily_values[-2], daily_values[-1]
    current, previous = daily[-1], daily[-2]

    return evaluate_values(
        current=current, previous=previous,
        recent_daily=daily[-config.swing_lookback:],
        monthly_rsi=monthly_rsi, weekly_rsi=weekly_rsi,
        previous_weekly_rsi=previous_weekly_rsi,
        previous_daily_rsi=previous_rsi, daily_rsi=daily_rsi,
        breadth_healthy=breadth_healthy, state=state, config=config,
    )


def evaluate_values(*, current: Candle, previous: Candle,
                    recent_daily: Sequence[Candle], monthly_rsi: float | None,
                    weekly_rsi: float | None, previous_weekly_rsi: float | None,
                    previous_daily_rsi: float | None, daily_rsi: float | None,
                    breadth_healthy: bool, state: State = State(),
                    config: Config = Config()) -> tuple[Decision, State]:
    """Evaluate precomputed indicator values; used by large-universe backtests."""
    previous_rsi = previous_daily_rsi

    if daily_rsi is None or previous_rsi is None:
        return Decision(Action.NONE, "insufficient_daily_rsi", monthly_rsi, weekly_rsi, daily_rsi), state

    if state.position_open:
        if daily_rsi < config.entry_rsi:
            decision = Decision(Action.EXIT, "daily_rsi_below_50", monthly_rsi, weekly_rsi, daily_rsi, stop=state.stop)
            return decision, State()
        if state.stop is not None and current.close < state.stop:
            decision = Decision(Action.EXIT, "close_below_swing_stop", monthly_rsi, weekly_rsi, daily_rsi, stop=state.stop)
            return decision, State()

    aligned = monthly_rsi is not None and weekly_rsi is not None and monthly_rsi > config.trend_rsi and weekly_rsi > config.trend_rsi
    weekly_rising = (not config.require_weekly_rsi_rising or
                     (previous_weekly_rsi is not None and weekly_rsi > previous_weekly_rsi))
    crossed = previous_rsi < config.entry_rsi <= daily_rsi
    bounced = config.entry_rsi <= previous_rsi <= config.bounce_ceiling and daily_rsi > previous_rsi
    pattern = bullish_pattern(previous, current)
    confirmed = daily_rsi >= config.confirmation_rsi
    eligible = aligned and weekly_rising and breadth_healthy and confirmed and (crossed or bounced) and pattern is not None

    if eligible and state.last_entry_day != current.day and state.entries < config.max_entries:
        pattern_stop = min(previous.low, current.low) if pattern == "bullish_engulfing" else current.low
        swing_stop = min(candle.low for candle in recent_daily)
        proposed_stop = min(pattern_stop, swing_stop)
        next_stop = max(state.stop, proposed_stop) if state.stop is not None else proposed_stop
        action = Action.ADD if state.position_open else Action.ENTER
        next_state = State(True, state.entries + 1, next_stop, current.day)
        reason = "rsi_50_cross" if crossed else "rsi_50_bounce"
        return Decision(action, reason, monthly_rsi, weekly_rsi, daily_rsi, pattern, next_stop), next_state

    if state.position_open:
        trailing_candidate = min(candle.low for candle in recent_daily)
        next_stop = max(state.stop, trailing_candidate) if state.stop is not None else trailing_candidate
        return Decision(Action.HOLD, "daily_rsi_above_50", monthly_rsi, weekly_rsi, daily_rsi, stop=next_stop), replace(state, stop=next_stop)

    if not aligned:
        reason = "monthly_weekly_rsi_not_aligned"
    elif not weekly_rising:
        reason = "weekly_rsi_not_rising"
    elif not breadth_healthy:
        reason = "market_breadth_unhealthy"
    elif not confirmed:
        reason = "daily_rsi_below_confirmation"
    elif not (crossed or bounced):
        reason = "no_daily_rsi_50_trigger"
    else:
        reason = "no_bullish_price_action"
    return Decision(Action.NONE, reason, monthly_rsi, weekly_rsi, daily_rsi, pattern), state
