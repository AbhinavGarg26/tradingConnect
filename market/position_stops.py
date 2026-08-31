"""Pure stop-loss state machine used by the live position monitor."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional


RECOVERY_BUFFER_PCT = 0.8
EMERGENCY_BUFFER_PCT = 2.0
SOFT_BREACH_WINDOW = timedelta(seconds=15)


@dataclass
class PositionStopState:
    peak_pnl_pct: float
    soft_breached_at: Optional[datetime] = None


class PositionStopTracker:
    def __init__(self):
        self._states: dict[str, PositionStopState] = {}

    def remove_missing(self, active_keys: set[str]) -> None:
        for key in set(self._states) - active_keys:
            self._states.pop(key, None)

    def evaluate(
        self,
        position_key: str,
        pnl_pct: float,
        soft_loss_pct: float,
        recent_prices: Iterable[float],
        now: Optional[datetime] = None,
    ) -> Optional[str]:
        """Return an exit reason, or None when the position should remain open."""
        now = now or datetime.now(timezone.utc)
        state = self._states.setdefault(
            position_key,
            PositionStopState(peak_pnl_pct=pnl_pct),
        )
        state.peak_pnl_pct = max(state.peak_pnl_pct, pnl_pct)

        locked_profit = self._locked_profit_pct(state.peak_pnl_pct)
        if locked_profit is not None and pnl_pct <= locked_profit:
            return f"PROFIT_LOCK_{locked_profit:.0f}PCT"

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

    @staticmethod
    def _locked_profit_pct(peak_pnl_pct: float) -> Optional[float]:
        if peak_pnl_pct < 5.0:
            return None
        if peak_pnl_pct < 10.0:
            return 1.0
        return 5.0 + (int((peak_pnl_pct - 10.0) // 5.0) * 5.0)

    @staticmethod
    def _has_positive_momentum(prices: Iterable[float]) -> bool:
        values = list(prices)[-5:]
        if len(values) < 3:
            return False
        upward_moves = sum(curr > prev for prev, curr in zip(values, values[1:]))
        return values[-1] > values[0] and upward_moves >= 2
