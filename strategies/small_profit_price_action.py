"""Conservative NIFTY CE/PE price-action strategy with daily P&L locks.

Signals are based on a completed 30-minute opening range, a breakout, a
retest, and a follow-through candle.  This module only decides; order
placement, fill confirmation, broker stops, and persisted P&L belong to the
runner.
"""
from decimal import Decimal

from strategies.fixed_profit import FixedProfit

STRATEGY_NAME = "small_profit_price_action"
STRATEGY_VERSION = "v1-paper"


class SmallProfitPriceAction(FixedProfit):
    """Price-action entries with a session profit lock and loss circuit breaker.

    The ₹5,000 value is a stop-trading threshold, not an expected or guaranteed
    daily return. ``record_closed_trade`` must receive actual option P&L after
    charges (or a conservative estimate of charges) before another entry.
    """

    def __init__(self, *, daily_profit_target=5000, max_daily_loss=2500,
                 max_consecutive_losses=2, max_entries=3, stop_points=8,
                 target_points=12, **kwargs):
        self.daily_profit_target = self._positive(daily_profit_target, "daily profit target")
        self.max_daily_loss = self._positive(max_daily_loss, "daily loss limit")
        if not isinstance(max_consecutive_losses, int) or max_consecutive_losses < 1:
            raise ValueError("Consecutive-loss limit must be a positive integer")
        self.max_consecutive_losses = max_consecutive_losses
        self.session_pnl = Decimal("0")
        self.consecutive_losses = 0
        self.lock_reason = None
        defaults = dict(min_range_points=20, confirmation_minutes=5,
                        tolerance_points=2, setup_mode="retest",
                        follow_through=True, entry_stop_points=8)
        defaults.update(kwargs)
        super().__init__(stop_points=stop_points, target_points=target_points,
                         max_entries=max_entries, **defaults)

    @staticmethod
    def _positive(value, label):
        value = Decimal(str(value))
        if not value.is_finite() or value <= 0:
            raise ValueError(f"{label.capitalize()} must be positive and finite")
        return value

    @property
    def can_enter(self):
        return self.lock_reason is None and self.entries < self.max_entries

    def set_reference(self, bar, *, observed_at):
        super().set_reference(bar, observed_at=observed_at)
        self.session_pnl = Decimal("0")
        self.consecutive_losses = 0
        self.lock_reason = None

    def on_bar(self, bar, *, observed_at):
        if not self.can_enter:
            return None
        return super().on_bar(bar, observed_at=observed_at)

    def record_closed_trade(self, net_option_pnl):
        """Record one fully closed trade before processing another entry."""
        if self.position or self.pending:
            raise ValueError("Confirm the exit before recording closed-trade P&L")
        pnl = Decimal(str(net_option_pnl))
        if not pnl.is_finite():
            raise ValueError("Closed-trade P&L must be finite")
        self.session_pnl += pnl
        self.consecutive_losses = self.consecutive_losses + 1 if pnl < 0 else 0
        if self.session_pnl >= self.daily_profit_target:
            self.lock_reason = "daily_profit_target"
        elif self.session_pnl <= -self.max_daily_loss:
            self.lock_reason = "max_daily_loss"
        elif self.consecutive_losses >= self.max_consecutive_losses:
            self.lock_reason = "consecutive_losses"
        elif self.entries >= self.max_entries:
            self.lock_reason = "max_entries"
        return self.lock_reason
