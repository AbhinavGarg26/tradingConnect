"""Broker-agnostic Smallcap Momentum Swing strategy and research signals.

The source setup uses a first-5-to-15-minute strong-start test. A daily-only
backtest cannot verify that test; see backtest/smallcap_momentum_swing.py for
the explicitly labelled daily-bar proxy.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import isfinite
from statistics import median
from typing import Sequence


KEY = "smallcap_momentum_swing"
VERSION = "v1-volume-pullback-strong-start"


@dataclass(frozen=True)
class Candle:
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def __post_init__(self):
        if not all(isfinite(value) for value in (self.open, self.high, self.low, self.close, self.volume)):
            raise ValueError("Candle values must be finite")
        if min(self.open, self.high, self.low, self.close) <= 0 or self.volume < 0:
            raise ValueError("Prices must be positive and volume cannot be negative")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("Candle high/low does not contain its body")


class Action(str, Enum):
    NONE = "NONE"
    SCAN = "SCAN"
    WATCH = "WATCH"
    ENTER = "ENTER"
    HOLD = "HOLD"
    PARTIAL_EXIT = "PARTIAL_EXIT"
    EXIT = "EXIT"


@dataclass(frozen=True)
class Config:
    scan_lookback: int = 20
    scan_volume_multiple: float = 2.0
    minimum_scan_day_return_pct: float = 5.0
    scan_return_volume_multiple: float = 2.5
    impulse_lookback: int = 60
    minimum_impulse_pct: float = 30.0
    maximum_pullback_pct: float = 25.0
    narrow_range_pct: float = 4.5
    narrow_vs_median: float = 0.65
    strong_start_volume_ratio: float = 1.5
    break_even_profit_pct: float = 8.0
    scale_out_move_pct: float = 50.0
    full_exit_move_pct: float = 70.0


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str
    entry_trigger: float | None = None
    initial_stop: float | None = None
    move_from_impulse_pct: float | None = None


def scan_for_focus(daily: Sequence[Candle], config: Config = Config()) -> Decision:
    """Flag a new 20-session high backed by unusually high turnover volume."""
    lookback = config.scan_lookback
    if len(daily) < lookback + 1:
        return Decision(Action.NONE, "insufficient_scan_history")
    today, previous = daily[-1], daily[-lookback - 1:-1]
    average_volume = sum(row.volume for row in previous) / lookback
    day_return = 100 * (today.close / daily[-2].close - 1)
    new_high = today.close > max(row.high for row in previous)
    volume_expansion = average_volume > 0 and today.volume >= config.scan_volume_multiple * average_volume
    strong_jump = (average_volume > 0 and day_return >= config.minimum_scan_day_return_pct
                   and today.volume >= config.scan_return_volume_multiple * average_volume)
    if new_high and (volume_expansion or strong_jump):
        return Decision(Action.SCAN, "new_20_session_high_with_exceptional_volume")
    return Decision(Action.NONE, "no_focus_list_trigger")


def evaluate_setup(daily: Sequence[Candle], *, previously_scanned: bool,
                   config: Config = Config()) -> Decision:
    """Find a controlled, contracting pullback in a name on the focus list."""
    if not previously_scanned:
        return Decision(Action.NONE, "not_on_focus_list")
    if len(daily) < max(config.impulse_lookback, 15):
        return Decision(Action.NONE, "insufficient_setup_history")
    window = daily[-config.impulse_lookback:]
    impulse_low = min(row.low for row in window)
    impulse_high = max(row.high for row in window)
    impulse_pct = 100 * (impulse_high / impulse_low - 1)
    if impulse_pct < config.minimum_impulse_pct:
        return Decision(Action.NONE, "no_qualifying_impulse")
    pullback_pct = 100 * (1 - daily[-1].close / impulse_high)
    if not 0 <= pullback_pct <= config.maximum_pullback_pct:
        return Decision(Action.NONE, "pullback_outside_limit")
    recent = daily[-5:]
    ranges_pct = [100 * (row.high - row.low) / row.close for row in recent]
    prior_ranges = [100 * (row.high - row.low) / row.close for row in daily[-15:-5]]
    contracting = ranges_pct[-1] <= config.narrow_range_pct and ranges_pct[-1] <= median(prior_ranges) * config.narrow_vs_median
    right_side = recent[-1].low >= recent[-2].low >= recent[-3].low
    firm_close = recent[-1].close >= recent[-1].low + .5 * (recent[-1].high - recent[-1].low)
    if contracting and right_side and firm_close:
        return Decision(Action.WATCH, "impulse_pullback_tightening_with_higher_lows")
    return Decision(Action.NONE, "base_not_tight_or_right_side_not_improving")


def confirm_strong_start(previous_day: Candle, opening_bars: Sequence[Candle], *,
                         same_time_volume_ratio: float, config: Config = Config()) -> Decision:
    """Confirm gap, hold, high break, and relative volume from opening bars."""
    if not opening_bars:
        return Decision(Action.NONE, "opening_bars_required")
    first = opening_bars[0]
    opening_low = min(row.low for row in opening_bars)
    opening_high = max(row.high for row in opening_bars)
    if first.open <= previous_day.high:
        return Decision(Action.NONE, "no_gap_above_previous_high")
    if opening_low < previous_day.low:
        return Decision(Action.NONE, "opening_move_failed_previous_low")
    if opening_high <= previous_day.high:
        return Decision(Action.NONE, "previous_high_not_broken")
    if same_time_volume_ratio < config.strong_start_volume_ratio:
        return Decision(Action.NONE, "opening_volume_not_confirmed")
    return Decision(Action.ENTER, "strong_start_gap_hold_break_with_volume",
                    entry_trigger=max(first.open, previous_day.high), initial_stop=opening_low)


def manage_position(*, entry_price: float, current_close: float, stop: float,
                    impulse_base: float, sma10: float | None, sma20: float | None,
                    scaled_out: bool = False, config: Config = Config()) -> Decision:
    """Apply the video-inspired break-even, moving-average, and scale-out plan."""
    if min(entry_price, current_close, stop, impulse_base) <= 0:
        raise ValueError("Position prices must be positive")
    gain_pct = 100 * (current_close / entry_price - 1)
    move_pct = 100 * (current_close / impulse_base - 1)
    if current_close <= stop:
        return Decision(Action.EXIT, "protective_stop_hit", move_from_impulse_pct=move_pct)
    if move_pct >= config.full_exit_move_pct:
        return Decision(Action.EXIT, "mature_impulse_move_extended", move_from_impulse_pct=move_pct)
    if move_pct >= config.scale_out_move_pct and not scaled_out:
        return Decision(Action.PARTIAL_EXIT, "scale_out_50_to_75pct_near_50pct_move",
                        move_from_impulse_pct=move_pct)
    candidates = [stop]
    if gain_pct >= config.break_even_profit_pct:
        candidates.append(entry_price)
    trail = sma20 if move_pct < 40 else sma10
    if trail is not None:
        candidates.append(trail)
    raised_stop = max(candidates)
    if move_pct >= 40:
        reason = "trail_with_sma10_mature_move"
    elif raised_stop == entry_price and raised_stop > stop:
        reason = "raise_stop_to_break_even"
    else:
        reason = "trail_with_sma20_young_move"
    return Decision(Action.HOLD, reason, initial_stop=raised_stop, move_from_impulse_pct=move_pct)
