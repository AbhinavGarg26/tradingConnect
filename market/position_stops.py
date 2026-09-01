"""Pure stop-loss state machine used by the live position monitor."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional


RECOVERY_BUFFER_PCT = 0.8
EMERGENCY_BUFFER_PCT = 2.0
SOFT_BREACH_WINDOW = timedelta(seconds=15)
PROFIT_ACTIVATION_PCT = 8.0
ATR_PROFIT_ACTIVATION_PCT = 15.0
PRE_PROFIT_ACTIVATION_PCT = 6.0
PROFIT_TRAIL_FROM_PEAK_PCT = 2.5
PROFIT_TRAIL_CONFIRMATION_WINDOW = timedelta(seconds=5)
PROFIT_HARD_FLOOR_PCT = 2.5
PRE_PROFIT_ARMING_BUFFER_PCT = 0.5


@dataclass
class PositionStopState:
    peak_pnl_pct: float
    worst_pnl_pct: float
    soft_breached_at: Optional[datetime] = None
    profit_breached_at: Optional[datetime] = None
    profit_breach_level: Optional[float] = None
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
        now: Optional[datetime] = None,
        charge_floor_pct: float = PROFIT_HARD_FLOOR_PCT,
        atr_trail_distance_pct: Optional[float] = None,
    ) -> Optional[str]:
        """Return an exit reason, or None when the position should remain open."""
        now = now or datetime.now(timezone.utc)
        state = self._states.setdefault(
            position_key,
            PositionStopState(peak_pnl_pct=pnl_pct, worst_pnl_pct=pnl_pct),
        )
        state.peak_pnl_pct = max(state.peak_pnl_pct, pnl_pct)
        state.worst_pnl_pct = min(state.worst_pnl_pct, pnl_pct)

        if (
            state.peak_pnl_pct >= ATR_PROFIT_ACTIVATION_PCT
            and atr_trail_distance_pct is not None
        ):
            state.atr_trail_active = True
            state.atr_trail_distance_pct = atr_trail_distance_pct
        locked_profit = self._locked_profit_pct(
            state.peak_pnl_pct,
            charge_floor_pct,
            state.atr_trail_distance_pct if state.atr_trail_active else None,
        )
        profit_exit = self._evaluate_profit_lock(
            state, pnl_pct, locked_profit, charge_floor_pct, now
        )
        if profit_exit:
            return profit_exit

        emergency_loss_pct = soft_loss_pct + EMERGENCY_BUFFER_PCT
        if pnl_pct <= -emergency_loss_pct:
            return "EMERGENCY_STOP"

        if state.soft_breached_at is None:
            if pnl_pct <= -soft_loss_pct:
                state.soft_breached_at = now
            return None

        recovery_level = -(soft_loss_pct - RECOVERY_BUFFER_PCT)
        if pnl_pct >= recovery_level and self._has_positive_momentum(recent_prices):
            state.soft_breached_at = None
            return None

        if now - state.soft_breached_at >= SOFT_BREACH_WINDOW:
            return "SOFT_STOP_TIMEOUT"
        return None

    def snapshot(self, position_key: str) -> Optional[dict]:
        state = self._states.get(position_key)
        if state is None:
            return None
        return {
            "peak_pnl_pct": state.peak_pnl_pct,
            "worst_pnl_pct": state.worst_pnl_pct,
            "soft_breached_at": (
                state.soft_breached_at.isoformat() if state.soft_breached_at else None
            ),
            "locked_profit_pct": state.profit_breach_level,
            "profit_breached_at": (
                state.profit_breached_at.isoformat() if state.profit_breached_at else None
            ),
            "profit_mode_active": state.peak_pnl_pct >= PROFIT_ACTIVATION_PCT,
            "pre_profit_mode_active": (
                state.profit_breach_level is not None
                and state.peak_pnl_pct < PROFIT_ACTIVATION_PCT
            ),
            "atr_trail_active": state.atr_trail_active,
            "atr_trail_distance_pct": state.atr_trail_distance_pct,
        }

    @staticmethod
    def _locked_profit_pct(
        peak_pnl_pct: float,
        charge_floor_pct: float = PROFIT_HARD_FLOOR_PCT,
        atr_trail_distance_pct: Optional[float] = None,
    ) -> Optional[float]:
        pre_profit_trigger = max(
            PRE_PROFIT_ACTIVATION_PCT,
            charge_floor_pct + PRE_PROFIT_ARMING_BUFFER_PCT,
        )
        if peak_pnl_pct < pre_profit_trigger:
            return None
        if peak_pnl_pct < PROFIT_ACTIVATION_PCT:
            return charge_floor_pct
        trail_distance = PROFIT_TRAIL_FROM_PEAK_PCT
        if peak_pnl_pct >= ATR_PROFIT_ACTIVATION_PCT and atr_trail_distance_pct is not None:
            trail_distance = max(PROFIT_TRAIL_FROM_PEAK_PCT, atr_trail_distance_pct)
        return max(PROFIT_HARD_FLOOR_PCT, peak_pnl_pct - trail_distance)

    @staticmethod
    def _evaluate_profit_lock(
        state: PositionStopState,
        pnl_pct: float,
        locked_profit: Optional[float],
        charge_floor_pct: float,
        now: datetime,
    ) -> Optional[str]:
        if locked_profit is None:
            state.profit_breached_at = None
            state.profit_breach_level = None
            return None

        # A volatility increase must never loosen profit already protected.
        if state.profit_breach_level is None:
            state.profit_breach_level = locked_profit
        else:
            state.profit_breach_level = max(state.profit_breach_level, locked_profit)
        active_floor = state.profit_breach_level
        if pnl_pct > active_floor:
            state.profit_breached_at = None
            return None

        full_profit_mode = state.peak_pnl_pct >= PROFIT_ACTIVATION_PCT
        reason = (
            "PROFIT_TRAIL_ATR"
            if state.atr_trail_active
            else "PROFIT_TRAIL_2.5PCT"
            if full_profit_mode
            else "PRE_PROFIT_CHARGE_FLOOR"
        )
        if full_profit_mode and pnl_pct <= max(PROFIT_HARD_FLOOR_PCT, charge_floor_pct):
            return f"{reason}_HARD_FLOOR"

        if state.profit_breached_at is None:
            state.profit_breached_at = now
            return None
        if now - state.profit_breached_at >= PROFIT_TRAIL_CONFIRMATION_WINDOW:
            return reason
        return None

    @staticmethod
    def _has_positive_momentum(prices: Iterable[float]) -> bool:
        values = list(prices)[-5:]
        if len(values) < 3:
            return False
        upward_moves = sum(curr > prev for prev, curr in zip(values, values[1:]))
        return values[-1] > values[0] and upward_moves >= 2
