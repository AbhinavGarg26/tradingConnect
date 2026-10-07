"""One-time recovery GTT rules shared by Zerodha and Groww long positions.

Rules are based on entry price, not broker-reported P&L.  The caller persists
the returned state so a service restart cannot recreate the same GTT event.
"""

from __future__ import annotations

from dataclasses import dataclass, field


LOSS_STEP = 5
LOSS_RECOVERY_OFFSET = 4
PROFIT_PEAK_STEP = 10
PROFIT_DRAWDOWN = 5
PROFIT_RECOVERY_OFFSET = 1


@dataclass
class RecoveryGttState:
    peak_pct: float = 0.0
    armed_loss_levels: set[int] = field(default_factory=set)
    armed_profit_peaks: set[int] = field(default_factory=set)
    profit_floor_10_armed: bool = False

    @classmethod
    def from_payload(cls, payload: dict | None) -> "RecoveryGttState":
        payload = payload or {}
        return cls(
            peak_pct=float(payload.get("peak_pct") or 0),
            armed_loss_levels={int(value) for value in payload.get("armed_loss_levels", [])},
            armed_profit_peaks={int(value) for value in payload.get("armed_profit_peaks", [])},
            profit_floor_10_armed=bool(payload.get("profit_floor_10_armed", False)),
        )

    def payload(self) -> dict:
        return {
            "peak_pct": round(self.peak_pct, 4),
            "armed_loss_levels": sorted(self.armed_loss_levels),
            "armed_profit_peaks": sorted(self.armed_profit_peaks),
            "profit_floor_10_armed": self.profit_floor_10_armed,
        }


@dataclass(frozen=True)
class RecoveryGttEvent:
    key: str
    kind: str
    trigger_pct: float
    observed_pct: float


def evaluate(
    entry_price: float,
    ltp: float,
    state: RecoveryGttState,
    allow_loss_recovery: bool = True,
) -> RecoveryGttEvent | None:
    """Return one new recovery event, or None when an existing event still holds.

    Examples: entry 100, LTP 95 -> loss-5 at 99.  Entry 100, after a
    10% peak and current LTP 105 -> profit-10 at 109.
    """
    if entry_price <= 0 or ltp <= 0:
        return None

    pnl_pct = ((ltp - entry_price) / entry_price) * 100
    state.peak_pct = max(state.peak_pct, pnl_pct)

    # Once +10% is observed, protect +5% at the broker immediately. This is a
    # downside GTT, not a software market exit, and is persisted across restarts.
    if state.peak_pct >= PROFIT_PEAK_STEP and not state.profit_floor_10_armed:
        state.profit_floor_10_armed = True
        return RecoveryGttEvent(
            key="profit-floor-10",
            kind="profit_floor",
            trigger_pct=5.0,
            observed_pct=pnl_pct,
        )

    loss_level = int(max(0, -pnl_pct) // LOSS_STEP)
    if allow_loss_recovery and loss_level >= 1 and loss_level not in state.armed_loss_levels:
        state.armed_loss_levels.add(loss_level)
        return RecoveryGttEvent(
            key=f"loss-{loss_level * LOSS_STEP}",
            kind="loss_recovery",
            trigger_pct=-(loss_level * LOSS_STEP - LOSS_RECOVERY_OFFSET),
            observed_pct=pnl_pct,
        )

    peak_band = int(state.peak_pct // PROFIT_PEAK_STEP) * PROFIT_PEAK_STEP
    if (
        allow_loss_recovery
        and peak_band >= PROFIT_PEAK_STEP * 2
        and pnl_pct <= peak_band - PROFIT_DRAWDOWN
        and peak_band not in state.armed_profit_peaks
    ):
        state.armed_profit_peaks.add(peak_band)
        return RecoveryGttEvent(
            key=f"profit-{peak_band}",
            kind="profit_recovery",
            trigger_pct=peak_band - PROFIT_RECOVERY_OFFSET,
            observed_pct=pnl_pct,
        )

    return None
