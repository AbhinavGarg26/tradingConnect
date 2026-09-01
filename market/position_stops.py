"""Pure peak-profit and hard-loss state machine for live positions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional


EMERGENCY_BUFFER_PCT = 2.0
FIRST_PEAK_PCT = 5.0
FIRST_PULLBACK_PCT = 2.0
FIRST_LIMIT_TARGET_PCT = 4.0
SECOND_PEAK_PCT = 10.0
SECOND_PULLBACK_PCT = 4.0
SECOND_LIMIT_TARGET_PCT = 7.0
PROFIT_HARD_FLOOR_PCT = 2.0
ATR_LIMIT_OFFSET_PCT = 4.0


@dataclass
class PositionStopState:
    peak_pnl_pct: float
    worst_pnl_pct: float
    profit_breach_level: Optional[float] = None
    profit_limit_target_pct: Optional[float] = None
    atr_trail_active: bool = False
    atr_trail_distance_pct: Optional[float] = None


class PositionStopTracker:
    def __init__(self):
        self._states: dict[str, PositionStopState] = {}

    def remove_missing(self, active_keys: set[str]) -> None:
        for key in set(self._states) - active_keys:
            self._states.pop(key, None)

    def reset(self, position_key: str) -> None:
        self._states.pop(position_key, None)

    def evaluate(
        self,
        position_key: str,
        pnl_pct: float,
        soft_loss_pct: float,
        recent_prices: Iterable[float],
        now=None,
        charge_floor_pct: float = 0.0,
        atr_trail_distance_pct: Optional[float] = None,
    ) -> Optional[str]:
        """Return an exit instruction reason, or None while holding."""
        del recent_prices, now, charge_floor_pct
        state = self._states.setdefault(
            position_key,
            PositionStopState(peak_pnl_pct=pnl_pct, worst_pnl_pct=pnl_pct),
        )
        state.peak_pnl_pct = max(state.peak_pnl_pct, pnl_pct)
        state.worst_pnl_pct = min(state.worst_pnl_pct, pnl_pct)

        emergency_loss_pct = soft_loss_pct + EMERGENCY_BUFFER_PCT
        if pnl_pct <= -emergency_loss_pct:
            return "EMERGENCY_STOP"

        # After a 10% peak, +2% remains the permanent protected market floor.
        if state.peak_pnl_pct >= SECOND_PEAK_PCT and pnl_pct <= PROFIT_HARD_FLOOR_PCT:
            return "PROFIT_HARD_FLOOR"

        if state.peak_pnl_pct > SECOND_PEAK_PCT and atr_trail_distance_pct is not None:
            state.atr_trail_active = True
            state.atr_trail_distance_pct = atr_trail_distance_pct
            candidate_floor = state.peak_pnl_pct - atr_trail_distance_pct
            if state.profit_breach_level is None:
                state.profit_breach_level = candidate_floor
            else:
                state.profit_breach_level = max(
                    state.profit_breach_level, candidate_floor
                )
            state.profit_limit_target_pct = (
                state.profit_breach_level + ATR_LIMIT_OFFSET_PCT
            )
            if pnl_pct <= state.profit_breach_level:
                return "PROFIT_ATR_RECOVERY_LIMIT"
            return None

        if state.peak_pnl_pct >= SECOND_PEAK_PCT:
            state.profit_breach_level = SECOND_PULLBACK_PCT
            state.profit_limit_target_pct = SECOND_LIMIT_TARGET_PCT
            if pnl_pct <= SECOND_PULLBACK_PCT:
                return "PROFIT_10PCT_RECOVERY_LIMIT"
            return None

        if state.peak_pnl_pct >= FIRST_PEAK_PCT:
            state.profit_breach_level = FIRST_PULLBACK_PCT
            state.profit_limit_target_pct = FIRST_LIMIT_TARGET_PCT
            if pnl_pct <= FIRST_PULLBACK_PCT:
                return "PROFIT_5PCT_RECOVERY_LIMIT"
            return None

        state.profit_breach_level = None
        state.profit_limit_target_pct = None
        return None

    def snapshot(self, position_key: str) -> Optional[dict]:
        state = self._states.get(position_key)
        if state is None:
            return None
        return {
            "peak_pnl_pct": state.peak_pnl_pct,
            "worst_pnl_pct": state.worst_pnl_pct,
            "soft_breached_at": None,
            "locked_profit_pct": state.profit_breach_level,
            "profit_breached_at": None,
            "profit_limit_target_pct": state.profit_limit_target_pct,
            "profit_mode_active": state.peak_pnl_pct >= FIRST_PEAK_PCT,
            "pre_profit_mode_active": False,
            "atr_trail_active": state.atr_trail_active,
            "atr_trail_distance_pct": state.atr_trail_distance_pct,
        }
