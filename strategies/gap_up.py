"""Deterministic gap-up strategies for equities and the NIFTY index.

The module is execution-neutral: it consumes completed candles and returns
decisions. Callers own data quality, order placement, sizing, and persistence.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from math import isfinite
from statistics import median
from typing import Sequence


STOCK_KEY = "stock_catalyst_gap_continuation"
INDEX_KEY = "nifty_gap_regime"
VERSION = "v1-15m-confirmed"


@dataclass(frozen=True)
class Bar:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def __post_init__(self):
        values = (self.open, self.high, self.low, self.close, self.volume)
        if not all(isfinite(value) for value in values):
            raise ValueError("Bar values must be finite")
        if min(self.open, self.high, self.low, self.close) <= 0 or self.volume < 0:
            raise ValueError("Prices must be positive and volume cannot be negative")
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError("Bar high/low does not contain its body")

    @property
    def width(self) -> float:
        return self.high - self.low


class Action(str, Enum):
    NONE = "NONE"
    WATCH = "WATCH"
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str
    trigger: float | None = None
    stop: float | None = None
    target_1: float | None = None
    target_2: float | None = None
    metadata: dict | None = None


@dataclass(frozen=True)
class StockGapConfig:
    min_gap_pct: float = 2.0
    max_gap_pct: float = 8.0
    max_prior_20d_return_pct: float = 30.0
    max_extension_from_ema20_pct: float = 15.0
    min_opening_relative_volume: float = 1.5
    min_gap_retained: float = 0.60
    min_opening_close_location: float = 0.75
    max_upper_wick_ratio: float = 0.35
    max_initial_risk_pct: float = 3.0
    minimum_price: float = 50.0


@dataclass(frozen=True)
class IndexGapConfig:
    min_gap_pct: float = 0.40
    max_gap_pct: float = 1.50
    continuation_gap_retained: float = 0.60
    continuation_breadth: float = 0.60
    rejection_breadth: float = 0.40
    reward_1: float = 1.0
    reward_2: float = 2.0


def _gap_pct(open_price: float, previous_close: float) -> float:
    return 100.0 * (open_price / previous_close - 1.0)


def _close_location(bar: Bar) -> float:
    return (bar.close - bar.low) / bar.width if bar.width else 0.5


def _upper_wick_ratio(bar: Bar) -> float:
    return (bar.high - max(bar.open, bar.close)) / bar.width if bar.width else 0.0


def _risk_decision(action: Action, reason: str, trigger: float, stop: float,
                   metadata: dict) -> Decision:
    risk = trigger - stop if action == Action.BUY else stop - trigger
    if risk <= 0:
        return Decision(Action.NONE, "invalid_nonpositive_risk", metadata=metadata)
    direction = 1.0 if action == Action.BUY else -1.0
    return Decision(action, reason, trigger, stop,
                    trigger + direction * risk, trigger + direction * 2 * risk,
                    metadata)


def stock_gap_watch(*, previous_close: float, previous_high: float,
                    today_open: float, prior_20d_close: float, ema20: float,
                    has_material_catalyst: bool, average_traded_value: float,
                    config: StockGapConfig = StockGapConfig()) -> Decision:
    """Qualify a stock before the opening confirmation is available."""
    gap = _gap_pct(today_open, previous_close)
    prior_return = 100 * (previous_close / prior_20d_close - 1)
    extension = 100 * (today_open / ema20 - 1)
    metadata = {"gap_pct": gap, "prior_20d_return_pct": prior_return,
                "extension_from_ema20_pct": extension}
    if today_open < config.minimum_price:
        return Decision(Action.NONE, "price_below_minimum", metadata=metadata)
    if average_traded_value <= 0:
        return Decision(Action.NONE, "missing_liquidity", metadata=metadata)
    if not has_material_catalyst:
        return Decision(Action.NONE, "no_verified_material_catalyst", metadata=metadata)
    if not config.min_gap_pct <= gap <= config.max_gap_pct:
        return Decision(Action.NONE, "gap_outside_stock_band", metadata=metadata)
    if today_open <= previous_high:
        return Decision(Action.NONE, "open_not_above_previous_high", metadata=metadata)
    if prior_return > config.max_prior_20d_return_pct:
        return Decision(Action.NONE, "prior_move_already_extended", metadata=metadata)
    if extension > config.max_extension_from_ema20_pct:
        return Decision(Action.NONE, "open_too_far_above_ema20", metadata=metadata)
    return Decision(Action.WATCH, "qualified_catalyst_gap", metadata=metadata)


def confirm_stock_continuation(*, previous_close: float, previous_low: float,
                               opening_15m: Bar, opening_relative_volume: float,
                               vwap: float, market_aligned: bool,
                               sector_aligned: bool,
                               config: StockGapConfig = StockGapConfig()) -> Decision:
    """Confirm a long stock entry from the completed first 15-minute bar."""
    gap_size = opening_15m.open - previous_close
    retained = ((opening_15m.close - previous_close) / gap_size
                if gap_size > 0 else float("-inf"))
    metadata = {"opening_relative_volume": opening_relative_volume,
                "gap_retained": retained, "vwap": vwap}
    if opening_relative_volume < config.min_opening_relative_volume:
        return Decision(Action.NONE, "opening_relative_volume_too_low", metadata=metadata)
    if opening_15m.close <= vwap:
        return Decision(Action.NONE, "opening_close_not_above_vwap", metadata=metadata)
    if _close_location(opening_15m) < config.min_opening_close_location:
        return Decision(Action.NONE, "opening_close_not_in_top_quarter", metadata=metadata)
    if _upper_wick_ratio(opening_15m) > config.max_upper_wick_ratio:
        return Decision(Action.NONE, "opening_upper_wick_too_large", metadata=metadata)
    if retained < config.min_gap_retained:
        return Decision(Action.NONE, "too_much_gap_retracement", metadata=metadata)
    if opening_15m.low < previous_low:
        return Decision(Action.NONE, "opening_failed_previous_low", metadata=metadata)
    if not market_aligned or not sector_aligned:
        return Decision(Action.NONE, "market_or_sector_not_aligned", metadata=metadata)
    trigger = opening_15m.high
    stop = max(opening_15m.low, vwap)
    risk_pct = 100 * (trigger / stop - 1)
    metadata["initial_risk_pct"] = risk_pct
    if risk_pct > config.max_initial_risk_pct:
        return Decision(Action.NONE, "initial_risk_too_wide", metadata=metadata)
    return _risk_decision(Action.BUY, "stock_15m_gap_continuation", trigger, stop, metadata)


def classify_index_gap(*, previous_close: float, previous_high: float,
                       opening_15m: Bar, vwap: float, breadth_above_vwap: float,
                       config: IndexGapConfig = IndexGapConfig()) -> Decision:
    """Choose continuation or rejection after the completed opening range."""
    gap = _gap_pct(opening_15m.open, previous_close)
    metadata = {"gap_pct": gap, "breadth_above_vwap": breadth_above_vwap,
                "vwap": vwap}
    if not 0 <= breadth_above_vwap <= 1:
        raise ValueError("Breadth must be between zero and one")
    if not config.min_gap_pct <= gap <= config.max_gap_pct:
        return Decision(Action.NONE, "gap_outside_index_band", metadata=metadata)
    gap_size = opening_15m.open - previous_close
    retained = (opening_15m.close - previous_close) / gap_size if gap_size > 0 else float("-inf")
    metadata["gap_retained"] = retained
    if (opening_15m.close > vwap and _close_location(opening_15m) >= 2 / 3
            and retained >= config.continuation_gap_retained
            and breadth_above_vwap >= config.continuation_breadth):
        stop = max(vwap, opening_15m.low + 0.5 * opening_15m.width)
        return _risk_decision(Action.BUY, "index_gap_continuation",
                              opening_15m.high, stop, metadata)
    if (opening_15m.close < vwap and opening_15m.close < opening_15m.low + 0.5 * opening_15m.width
            and opening_15m.close < previous_high
            and breadth_above_vwap <= config.rejection_breadth):
        return _risk_decision(Action.SELL, "index_gap_rejection",
                              opening_15m.low, opening_15m.high, metadata)
    return Decision(Action.NONE, "opening_regime_unconfirmed", metadata=metadata)


def opening_relative_volume(current_opening_volume: float,
                            historical_same_window_volumes: Sequence[float]) -> float:
    """Compare opening volume with the prior same-window median."""
    valid = [float(value) for value in historical_same_window_volumes
             if isfinite(float(value)) and float(value) > 0]
    if len(valid) < 10:
        raise ValueError("At least ten positive historical opening volumes are required")
    baseline = median(valid[-20:])
    return float(current_opening_volume) / baseline
