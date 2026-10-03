"""Pure peak-profit and hard-loss state machine for live positions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional


HARD_STOP_LOSS_PCT = 12.0
PROFIT_TARGET_PCT = 15.0
PROFIT_LADDER_START_PCT = 10.0
PROFIT_LADDER_STEP_PCT = 5.0
PROFIT_LOCK_STEP_PCT = 2.5
FIFTY_PCT_PEAK = 50.0
FIFTY_PCT_LOCK_INCREMENT = 20.0
HUNDRED_PCT_PEAK = 100.0
HUNDRED_PCT_LOCK_INCREMENT = 30.0


def locked_profit_for_peak(peak_pnl_pct: float) -> Optional[float]:
    """Return the protected P&L floor for the highest completed 5% milestone.

    Normal milestones add 2.5 points. At 50% and 100%, the larger requested
    increments are added to the floor already earned at the prior milestone.
    """
    if peak_pnl_pct < PROFIT_LADDER_START_PCT:
        return None

    # 10..45%: 2.5, 5, ... 20.
    pre_fifty_milestones = int(
        (min(peak_pnl_pct, FIFTY_PCT_PEAK - PROFIT_LADDER_STEP_PCT)
         - PROFIT_LADDER_START_PCT) // PROFIT_LADDER_STEP_PCT
    )
    locked_profit = PROFIT_LOCK_STEP_PCT * (pre_fifty_milestones + 1)

    if peak_pnl_pct >= FIFTY_PCT_PEAK:
        # At 50%, add 20 points to the 20% already locked at the 45% milestone.
        locked_profit += FIFTY_PCT_LOCK_INCREMENT
        locked_profit += (
            int((min(peak_pnl_pct, HUNDRED_PCT_PEAK - PROFIT_LADDER_STEP_PCT)
                 - FIFTY_PCT_PEAK) // PROFIT_LADDER_STEP_PCT)
            * PROFIT_LOCK_STEP_PCT
        )

    if peak_pnl_pct >= HUNDRED_PCT_PEAK:
        # At 100%, add 30 points to the 62.5% floor earned at the 95% milestone.
        locked_profit += HUNDRED_PCT_LOCK_INCREMENT
        locked_profit += (
            int((peak_pnl_pct - HUNDRED_PCT_PEAK) // PROFIT_LADDER_STEP_PCT)
            * PROFIT_LOCK_STEP_PCT
        )

    return locked_profit


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
        del recent_prices, now, charge_floor_pct, soft_loss_pct
        state = self._states.setdefault(
            position_key,
            PositionStopState(peak_pnl_pct=pnl_pct, worst_pnl_pct=pnl_pct),
        )
        state.peak_pnl_pct = max(state.peak_pnl_pct, pnl_pct)
        state.worst_pnl_pct = min(state.worst_pnl_pct, pnl_pct)

        # These two absolute risk boundaries take priority over all trailing
        # logic.  They intentionally produce market exits.
        if pnl_pct <= -HARD_STOP_LOSS_PCT:
            return "HARD_STOP_12PCT"
        if pnl_pct >= PROFIT_TARGET_PCT:
            return "PROFIT_TARGET_15PCT"

        # Arm the requested profit ladder from the highest observed P&L peak.
        del atr_trail_distance_pct
        requested_floor = locked_profit_for_peak(state.peak_pnl_pct)
        if requested_floor is not None:
            state.profit_breach_level = max(
                state.profit_breach_level or requested_floor,
                requested_floor,
            )
            state.profit_limit_target_pct = None
            if pnl_pct <= state.profit_breach_level:
                return "PROFIT_LADDER_STOP"
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
            "profit_mode_active": state.peak_pnl_pct >= PROFIT_LADDER_START_PCT,
            "pre_profit_mode_active": False,
            "atr_trail_active": state.atr_trail_active,
            "atr_trail_distance_pct": state.atr_trail_distance_pct,
        }
