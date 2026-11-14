"""One-direction trend and profit-protection overlay (paper only).

This module does not create an entry edge.  It decides whether a signal from a
separately tested strategy is allowed, and calculates a protective option stop
after profit appears.  Broker orders and durable session state belong to the
runner.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_DOWN
from typing import Literal

from strategies.nifty_opening_range import IST

Direction = Literal["CE", "PE"]
STRATEGY_NAME = "one_direction_profit_lock"
STRATEGY_VERSION = "v2.1-paper"


@dataclass(frozen=True)
class TrendSnapshot:
    observed_at: datetime
    close: Decimal
    ema_fast: Decimal
    ema_slow: Decimal
    vwap: Decimal
    adx: Decimal
    plus_di: Decimal
    minus_di: Decimal


class OneDirectionProfitLock:
    """Fail-closed session gate for a separately validated entry strategy."""

    def __init__(
        self,
        *,
        max_entries: int = 3,
        max_consecutive_losses: int = 2,
        daily_profit_lock: Decimal | int = 2000,
        max_daily_loss: Decimal | int = 3000,
        min_adx: Decimal | int = 25,
        min_di_gap: Decimal | int = 8,
        entry_cutoff: time = time(12, 0),
        profit_stop_trigger_pct: Decimal | int = 10,
        profit_stop_lock_pct: Decimal | int = 5,
        max_loss_per_trade: Decimal | int = 1000,
        session_profit_activation: Decimal | int = 5000,
        session_profit_retain_pct: Decimal | int = 70,
        reversal_confirmations: int = 2,
        reversal_cooldown_minutes: int = 15,
    ):
        if not isinstance(max_entries, int) or max_entries < 1:
            raise ValueError("max_entries must be a positive integer")
        if not isinstance(max_consecutive_losses, int) or max_consecutive_losses < 1:
            raise ValueError("max_consecutive_losses must be a positive integer")
        self.max_entries = max_entries
        self.max_consecutive_losses = max_consecutive_losses
        self.daily_profit_lock = self._positive(daily_profit_lock, "daily_profit_lock")
        self.max_daily_loss = self._positive(max_daily_loss, "max_daily_loss")
        self.min_adx = self._positive(min_adx, "min_adx")
        self.min_di_gap = self._positive(min_di_gap, "min_di_gap")
        self.profit_stop_trigger_pct = self._positive(
            profit_stop_trigger_pct, "profit_stop_trigger_pct"
        )
        self.profit_stop_lock_pct = self._positive(
            profit_stop_lock_pct, "profit_stop_lock_pct"
        )
        self.max_loss_per_trade = self._positive(max_loss_per_trade, "max_loss_per_trade")
        self.session_profit_activation = self._positive(
            session_profit_activation, "session_profit_activation"
        )
        self.session_profit_retain_pct = self._positive(
            session_profit_retain_pct, "session_profit_retain_pct"
        )
        if self.session_profit_retain_pct >= 100:
            raise ValueError("session_profit_retain_pct must be below 100")
        if not isinstance(reversal_confirmations, int) or reversal_confirmations < 2:
            raise ValueError("reversal_confirmations must be at least two")
        if not isinstance(reversal_cooldown_minutes, int) or reversal_cooldown_minutes < 1:
            raise ValueError("reversal_cooldown_minutes must be positive")
        self.reversal_confirmations = reversal_confirmations
        self.reversal_cooldown = timedelta(minutes=reversal_cooldown_minutes)
        if self.profit_stop_lock_pct >= self.profit_stop_trigger_pct:
            raise ValueError("profit stop lock must be below its activation trigger")
        self.entry_cutoff = entry_cutoff
        self.reset()

    @staticmethod
    def _positive(value, label: str) -> Decimal:
        result = Decimal(str(value))
        if not result.is_finite() or result <= 0:
            raise ValueError(f"{label} must be positive and finite")
        return result

    def reset(self) -> None:
        self.direction: Direction | None = None
        self.entries = 0
        self.session_pnl = Decimal("0")
        self.peak_closed_pnl = Decimal("0")
        self.consecutive_losses = 0
        self.lock_reason: str | None = None
        self.session_peak_pnl = Decimal("0")
        self.session_profit_floor: Decimal | None = None
        self.reversals = 0
        self._reversal_candidate: Direction | None = None
        self._reversal_count = 0
        self.last_exit_at: datetime | None = None
        self.position_open = False

    def market_direction(self, snapshot: TrendSnapshot) -> Direction | None:
        values = (
            snapshot.close, snapshot.ema_fast, snapshot.ema_slow, snapshot.vwap,
            snapshot.adx, snapshot.plus_di, snapshot.minus_di,
        )
        if snapshot.observed_at.tzinfo is None or not all(v.is_finite() and v > 0 for v in values):
            return None
        if snapshot.adx < self.min_adx:
            return None
        if (
            snapshot.close > snapshot.vwap
            and snapshot.ema_fast > snapshot.ema_slow
            and snapshot.plus_di - snapshot.minus_di >= self.min_di_gap
        ):
            return "CE"
        if (
            snapshot.close < snapshot.vwap
            and snapshot.ema_fast < snapshot.ema_slow
            and snapshot.minus_di - snapshot.plus_di >= self.min_di_gap
        ):
            return "PE"
        return None

    def allow_entry(self, requested: Direction, snapshot: TrendSnapshot) -> tuple[bool, str]:
        if requested not in {"CE", "PE"}:
            return False, "invalid_direction"
        if self.lock_reason:
            return False, self.lock_reason
        if self.position_open:
            return False, "position_open"
        if snapshot.observed_at.astimezone(IST).time() >= self.entry_cutoff:
            return False, "entry_cutoff"
        if self.entries >= self.max_entries:
            return False, "max_entries"
        flowing = self.market_direction(snapshot)
        if flowing is None:
            return False, "no_confirmed_trend"
        if requested != flowing:
            return False, "against_market_flow"
        if self.direction is not None and requested != self.direction:
            return False, "session_direction_locked"
        return True, "allowed"

    def record_entry(self, direction: Direction, snapshot: TrendSnapshot) -> None:
        allowed, reason = self.allow_entry(direction, snapshot)
        if not allowed:
            raise ValueError(f"entry blocked: {reason}")
        self.direction = self.direction or direction
        self.entries += 1
        self.position_open = True

    def record_closed_trade(self, net_pnl) -> str | None:
        pnl = Decimal(str(net_pnl))
        if not pnl.is_finite():
            raise ValueError("net_pnl must be finite")
        self.position_open = False
        self.session_pnl += pnl
        self.peak_closed_pnl = max(self.peak_closed_pnl, self.session_pnl)
        self.consecutive_losses = self.consecutive_losses + 1 if pnl < 0 else 0
        if self.session_pnl >= self.daily_profit_lock:
            self.lock_reason = "daily_profit_locked"
        elif self.session_pnl <= -self.max_daily_loss:
            self.lock_reason = "max_daily_loss"
        elif self.consecutive_losses >= self.max_consecutive_losses:
            self.lock_reason = "consecutive_losses"
        elif self.entries >= self.max_entries:
            self.lock_reason = "max_entries"
        return self.lock_reason

    def update_session_pnl(self, current_net_pnl) -> str | None:
        """Track realized + unrealized account P&L and enforce its peak floor."""
        current = Decimal(str(current_net_pnl))
        if not current.is_finite():
            raise ValueError("current_net_pnl must be finite")
        self.session_peak_pnl = max(self.session_peak_pnl, current)
        if self.session_peak_pnl >= self.session_profit_activation:
            floor = self.session_peak_pnl * self.session_profit_retain_pct / Decimal("100")
            self.session_profit_floor = max(self.session_profit_floor or floor, floor)
            if current <= self.session_profit_floor:
                self.lock_reason = "session_profit_floor"
        if current <= -self.max_daily_loss:
            self.lock_reason = "max_daily_loss"
        return self.lock_reason

    def initial_stop(self, entry_price, stop_loss_pct=10) -> Decimal:
        entry = self._positive(entry_price, "entry_price")
        stop_pct = self._positive(stop_loss_pct, "stop_loss_pct")
        if stop_pct >= 100:
            raise ValueError("stop_loss_pct must be below 100")
        return (entry * (Decimal("1") - stop_pct / Decimal("100"))).quantize(Decimal("0.05"))

    def risk_sized_quantity(self, entry_price, initial_stop, lot_size: int) -> int:
        """Largest whole-lot quantity whose planned loss is within the cap."""
        entry = self._positive(entry_price, "entry_price")
        stop = self._positive(initial_stop, "initial_stop")
        if stop >= entry or not isinstance(lot_size, int) or lot_size < 1:
            raise ValueError("stop must be below entry and lot_size must be positive")
        loss_per_lot = (entry - stop) * lot_size
        lots = int((self.max_loss_per_trade / loss_per_lot).to_integral_value(rounding=ROUND_DOWN))
        if lots < 1:
            raise ValueError("one lot exceeds max_loss_per_trade")
        return lots * lot_size

    def record_exit(self, exited_at: datetime) -> None:
        if exited_at.tzinfo is None:
            raise ValueError("exited_at must be timezone-aware")
        self.last_exit_at = exited_at
        self.position_open = False

    def observe_reversal(self, snapshot: TrendSnapshot) -> Direction | None:
        """Return a confirmed opposite side after consecutive closed candles."""
        flowing = self.market_direction(snapshot)
        if self.direction is None or flowing is None or flowing == self.direction:
            self._reversal_candidate = None
            self._reversal_count = 0
            return None
        if self.reversals >= 1 or self.lock_reason:
            return None
        if self.last_exit_at and snapshot.observed_at < self.last_exit_at + self.reversal_cooldown:
            return None
        if flowing != self._reversal_candidate:
            self._reversal_candidate = flowing
            self._reversal_count = 1
        else:
            self._reversal_count += 1
        return flowing if self._reversal_count >= self.reversal_confirmations else None

    def accept_reversal(self, direction: Direction, observed_at: datetime) -> None:
        if direction != self._reversal_candidate or self._reversal_count < self.reversal_confirmations:
            raise ValueError("reversal is not confirmed")
        if self.last_exit_at and observed_at < self.last_exit_at + self.reversal_cooldown:
            raise ValueError("reversal cooldown is active")
        if self.reversals >= 1:
            raise ValueError("daily reversal limit reached")
        self.direction = direction
        self.reversals += 1
        self._reversal_candidate = None
        self._reversal_count = 0

    def state(self) -> dict:
        """Serializable state that a runner must persist after every change."""
        return {
            "direction": self.direction,
            "entries": self.entries,
            "session_pnl": str(self.session_pnl),
            "peak_closed_pnl": str(self.peak_closed_pnl),
            "consecutive_losses": self.consecutive_losses,
            "lock_reason": self.lock_reason,
            "session_peak_pnl": str(self.session_peak_pnl),
            "session_profit_floor": (
                str(self.session_profit_floor) if self.session_profit_floor is not None else None
            ),
            "reversals": self.reversals,
            "last_exit_at": self.last_exit_at.isoformat() if self.last_exit_at else None,
            "position_open": self.position_open,
        }

    def restore(self, state: dict) -> None:
        """Restore validated durable state; malformed input fails closed."""
        try:
            direction = state.get("direction")
            if direction not in {None, "CE", "PE"}:
                raise ValueError
            self.direction = direction
            self.entries = int(state["entries"])
            self.session_pnl = Decimal(state["session_pnl"])
            self.peak_closed_pnl = Decimal(state["peak_closed_pnl"])
            self.consecutive_losses = int(state["consecutive_losses"])
            self.lock_reason = state.get("lock_reason")
            self.session_peak_pnl = Decimal(state["session_peak_pnl"])
            floor = state.get("session_profit_floor")
            self.session_profit_floor = Decimal(floor) if floor is not None else None
            self.reversals = int(state["reversals"])
            exited = state.get("last_exit_at")
            self.last_exit_at = datetime.fromisoformat(exited) if exited else None
            self.position_open = bool(state["position_open"])
            if not (0 <= self.entries <= self.max_entries and 0 <= self.reversals <= 1):
                raise ValueError
        except (KeyError, TypeError, ValueError, ArithmeticError):
            self.lock_reason = "invalid_persisted_state"
            raise ValueError("invalid persisted strategy state")

    def protected_stop(self, entry_price, peak_price, current_stop=None) -> Decimal | None:
        """Return a sell-stop trigger after a long option gains enough.

        At +10% peak option profit the default stop locks +5%.  Above +10%,
        half of the additional peak gain is added to the locked return.  The
        stop never moves down when ``current_stop`` is supplied.
        """
        entry = self._positive(entry_price, "entry_price")
        peak = self._positive(peak_price, "peak_price")
        gain_pct = (peak / entry - Decimal("1")) * Decimal("100")
        if gain_pct < self.profit_stop_trigger_pct:
            return None
        locked_pct = self.profit_stop_lock_pct + (
            gain_pct - self.profit_stop_trigger_pct
        ) / Decimal("2")
        candidate = (entry * (Decimal("1") + locked_pct / Decimal("100"))).quantize(
            Decimal("0.05")
        )
        if current_stop is None:
            return candidate
        current = Decimal(str(current_stop))
        return max(candidate, current)
