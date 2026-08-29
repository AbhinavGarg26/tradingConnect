"""
trail_methods.py — five trailing SL strategies.

Each method exposes a single classmethod:
    compute_new_sl(trade, candles, config) -> TrailResult

TrailResult.new_sl is None if no trail is warranted yet.
The caller (StrategyEngine) decides whether to apply the result.

Methods:
  - ATRTrail         — SL = current price ± (ATR × multiplier)
  - SwingTrail       — SL = last swing low/high over lookback window
  - RMultipleTrail   — SL steps up at 1R → breakeven, 2R → lock 1R, 3R → lock 2R
  - EMATrail         — SL = EMA(period) value
  - SupportRejection — detects rejection from user-defined support, emits sell signal
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import List, Optional

from trading.candle_builder import Candle
from trading.candle_store import ATRCalculator, EMACalculator, SwingCalculator
from trading.models import SupportLevel, Trade


# ─────────────────────────────────────────────────────────────
# TrailResult
# ─────────────────────────────────────────────────────────────

@dataclass
class TrailResult:
    new_sl:         Optional[Decimal]   # None = no trail needed
    method:         str
    trigger_reason: str
    atr_value:      Optional[Decimal]   = None
    r_multiple:     Optional[Decimal]   = None
    should_exit:    bool                = False  # True = close trade now (support rejection)
    exit_reason:    Optional[str]       = None


# ─────────────────────────────────────────────────────────────
# 1. ATR Trail
# ─────────────────────────────────────────────────────────────

class ATRTrail:
    """
    Trail SL at:
      BUY  → current_close - (ATR × multiplier)
      SELL → current_close + (ATR × multiplier)

    Only moves SL in the favourable direction (never tightens against trade).
    """

    @classmethod
    def compute_new_sl(
        cls,
        trade: Trade,
        candles: List[Candle],
        atr_period: int     = 14,
        atr_multiplier: float = 1.5,
    ) -> TrailResult:
        atr = ATRCalculator.compute(candles, period=atr_period)
        if atr is None:
            return TrailResult(None, "atr", "insufficient_data", atr_value=None)

        current_close = candles[-1].close
        multiplier    = Decimal(str(atr_multiplier))

        if trade.direction == "BUY":
            candidate = current_close - (atr * multiplier)
            if candidate > trade.current_sl:
                return TrailResult(
                    new_sl=candidate.quantize(Decimal("0.05")),
                    method="atr",
                    trigger_reason="atr_expand",
                    atr_value=atr,
                )
        else:  # SELL
            candidate = current_close + (atr * multiplier)
            if candidate < trade.current_sl:
                return TrailResult(
                    new_sl=candidate.quantize(Decimal("0.05")),
                    method="atr",
                    trigger_reason="atr_expand",
                    atr_value=atr,
                )

        return TrailResult(None, "atr", "no_improvement", atr_value=atr)


# ─────────────────────────────────────────────────────────────
# 2. Swing High / Low Trail
# ─────────────────────────────────────────────────────────────

class SwingTrail:
    """
    Trail SL at the last swing low (BUY) or swing high (SELL)
    over a rolling lookback window of candles.

    Avoids round-number SL by using the raw swing price.
    """

    @classmethod
    def compute_new_sl(
        cls,
        trade: Trade,
        candles: List[Candle],
        lookback: int = 5,
    ) -> TrailResult:
        if len(candles) < lookback:
            return TrailResult(None, "swing", "insufficient_data")

        if trade.direction == "BUY":
            swing = SwingCalculator.last_swing_low(candles, lookback)
            if swing and swing > trade.current_sl:
                return TrailResult(
                    new_sl=swing,
                    method="swing",
                    trigger_reason="new_swing_low",
                )
        else:
            swing = SwingCalculator.last_swing_high(candles, lookback)
            if swing and swing < trade.current_sl:
                return TrailResult(
                    new_sl=swing,
                    method="swing",
                    trigger_reason="new_swing_high",
                )

        return TrailResult(None, "swing", "no_new_swing")


# ─────────────────────────────────────────────────────────────
# 3. R-Multiple Trail
# ─────────────────────────────────────────────────────────────

class RMultipleTrail:
    """
    Step-based trail using risk multiples:

      At 1R profit → move SL to breakeven (entry price)
      At 2R profit → lock in 1R profit   (entry + 1 × risk_per_unit)
      At 3R profit → lock in 2R profit   (entry + 2 × risk_per_unit)

    risk_per_unit = abs(entry_price - initial_sl)

    Never moves SL backwards — only steps up at each R milestone.
    """

    R_STEPS = [
        (Decimal("3"), Decimal("2"), "r3_hit"),
        (Decimal("2"), Decimal("1"), "r2_hit"),
        (Decimal("1"), Decimal("0"), "r1_hit"),   # breakeven
    ]

    @classmethod
    def compute_new_sl(
        cls,
        trade: Trade,
        candles: List[Candle],
    ) -> TrailResult:
        if not trade.risk_amount or trade.risk_amount == 0 or not trade.quantity:
            return TrailResult(None, "r_multiple", "no_risk_defined")

        current_close  = candles[-1].close
        risk_per_unit  = abs(trade.entry_price - trade.initial_sl)

        if risk_per_unit == 0:
            return TrailResult(None, "r_multiple", "zero_risk_per_unit")

        # Current R achieved
        if trade.direction == "BUY":
            current_r = (current_close - trade.entry_price) / risk_per_unit
        else:
            current_r = (trade.entry_price - current_close) / risk_per_unit

        print(
            f"  RMultiple check: current_r={current_r:.2f} current_sl={trade.current_sl} entry={trade.entry_price} risk_per_unit={risk_per_unit}")
        # Walk through R steps highest → lowest, apply first qualifying step
        for trigger_r, lock_r, reason in cls.R_STEPS:
            if current_r >= trigger_r:
                if trade.direction == "BUY":
                    candidate = trade.entry_price + (lock_r * risk_per_unit)
                else:
                    candidate = trade.entry_price - (lock_r * risk_per_unit)

                # Only apply if it improves the current SL
                improves = (
                    (trade.direction == "BUY"  and candidate > trade.current_sl) or
                    (trade.direction == "SELL" and candidate < trade.current_sl)
                )
                if improves:
                    return TrailResult(
                        new_sl=candidate.quantize(Decimal("0.05")),
                        method="r_multiple",
                        trigger_reason=reason,
                        r_multiple=current_r.quantize(Decimal("0.01")),
                    )
                break  # already at or past this step — no further improvement

        return TrailResult(
            None, "r_multiple", "no_r_milestone",
            r_multiple=current_r.quantize(Decimal("0.01")),
        )


# ─────────────────────────────────────────────────────────────
# 4. EMA Trail
# ─────────────────────────────────────────────────────────────

class EMATrail:
    """
    Trail SL at the EMA(period) of close prices.

    BUY  → SL = EMA value (moves up as EMA rises)
    SELL → SL = EMA value (moves down as EMA falls)

    Common use: 9 EMA for aggressive trail, 21 EMA for relaxed trail.
    Period configured via system_configs key "ema_trail_period".
    """

    @classmethod
    def compute_new_sl(
        cls,
        trade: Trade,
        candles: List[Candle],
        period: int = 21,
    ) -> TrailResult:
        ema = EMACalculator.compute(candles, period=period)
        if ema is None:
            return TrailResult(None, "ema", "insufficient_data")

        if trade.direction == "BUY":
            if ema > trade.current_sl:
                return TrailResult(
                    new_sl=ema,
                    method="ema",
                    trigger_reason=f"ema{period}_rising",
                )
        else:
            if ema < trade.current_sl:
                return TrailResult(
                    new_sl=ema,
                    method="ema",
                    trigger_reason=f"ema{period}_falling",
                )

        return TrailResult(None, "ema", "no_improvement")


# ─────────────────────────────────────────────────────────────
# 5. Support Rejection
# ─────────────────────────────────────────────────────────────

class SupportRejectionDetector:
    """
    Detects price rejection from user-defined support levels.

    Two valid rejection patterns (as configured):
      A) Wick + close above support:
         — Candle low touched the support zone (wick)
         — Candle close is above the support level
         — Sets pending_confirmation = True

      B) Just price touch and bounce:
         — Price came within buffer% of support
         — Current close is above support
         — Sets pending_confirmation = True

    Confirmation (next candle):
      — If pending_confirmation is True and the NEXT candle
        also closes above support → emit exit signal

    State:
      _pending dict maps (trade_id, level_id) → candle that triggered
      Cleared on confirmation or if price breaks back below support.
    """

    def __init__(self, zone_buffer_pct: float = 0.3):
        self.zone_buffer_pct = zone_buffer_pct
        # {(trade_id, level_id): triggering_candle_close}
        self._pending: dict = {}

    def evaluate(
        self,
        trade: Trade,
        candle: Candle,
        prev_candle: Optional[Candle],
        support_levels: List[SupportLevel],
    ) -> TrailResult:
        """
        Call on every closed candle for trades that have active support levels.
        Returns TrailResult with should_exit=True when rejection is confirmed.
        """
        if not support_levels or prev_candle is None:
            return TrailResult(None, "support_rejection", "no_levels")

        current_close = Decimal(str(candle.close))
        prev_low      = Decimal(str(prev_candle.low))
        prev_close    = Decimal(str(prev_candle.close))

        for level in support_levels:
            if not level.is_currently_valid:
                continue

            key = (trade.id, level.id)

            # ── Check if previously pending confirmation ──────
            if key in self._pending:
                if current_close > level.price_level:
                    # Confirmed — two candles closed above support after rejection
                    del self._pending[key]
                    return TrailResult(
                        new_sl=None,
                        method="support_rejection",
                        trigger_reason="rejection_confirmed",
                        should_exit=True,
                        exit_reason="support_rejection",
                    )
                else:
                    # Price broke back below — cancel pending
                    del self._pending[key]
                    continue

            # ── Pattern A: wick + close above support ─────────
            wick_touched = self._wick_touched(prev_low, level)
            close_above  = prev_close > level.price_level

            if wick_touched and close_above:
                self._pending[key] = prev_close
                continue

            # ── Pattern B: price touch and bounce ─────────────
            near_support = level.is_price_near(prev_low, self.zone_buffer_pct)
            bounced      = prev_close > level.price_level

            if near_support and bounced:
                self._pending[key] = prev_close
                continue

        return TrailResult(None, "support_rejection", "no_rejection_detected")

    def _wick_touched(self, candle_low: Decimal, level: SupportLevel) -> bool:
        """True if the candle wick reached into or below the support zone."""
        if level.zone_lower:
            return candle_low <= level.zone_upper
        buffer = level.price_level * Decimal(str(self.zone_buffer_pct / 100))
        return candle_low <= level.price_level + buffer

    def clear_trade(self, trade_id) -> None:
        """Remove all pending state for a closed trade."""
        keys = [k for k in self._pending if k[0] == trade_id]
        for k in keys:
            del self._pending[k]