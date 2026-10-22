"""Shared, fail-closed risk settings for live strategy-owned option positions.

This module is strategy-agnostic. A runner must call ``check_new_entry`` before
each order and the independent guardian must keep its heartbeat current.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Numeric, String, Text, func, select
from sqlalchemy.orm import Mapped, mapped_column

from trading.database import Base, get_db
from trading.strategy_records import StrategyTrade


class RiskBlocked(RuntimeError):
    """New entries must stop without placing an order."""


class StrategyRiskSetting(Base):
    __tablename__ = "strategy_risk_settings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    strategy_key: Mapped[str] = mapped_column(String, nullable=False)
    amount_allocated: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    risk_per_trade: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    max_daily_loss: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    stop_limit_buffer_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    stop_mode: Mapped[str] = mapped_column(String, nullable=False)
    adverse_action: Mapped[str] = mapped_column(String, nullable=False)
    live_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)


class StrategyGuardianStatus(Base):
    __tablename__ = "strategy_guardian_statuses"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    strategy_key: Mapped[str] = mapped_column(String, nullable=False)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String, nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


@dataclass(frozen=True)
class Limits:
    amount_allocated: Decimal
    risk_per_trade: Decimal
    max_daily_loss: Decimal
    stop_limit_buffer_pct: Decimal
    stop_mode: str
    adverse_action: str


def load_limits(user_id: int, strategy_key: str) -> Limits:
    with get_db() as db:
        setting = db.scalar(select(StrategyRiskSetting).where(
            StrategyRiskSetting.user_id == user_id,
            StrategyRiskSetting.strategy_key == strategy_key,
        ))
        if setting is None or not setting.live_enabled:
            raise RiskBlocked("Live entries disabled until strategy risk settings are saved and enabled")
        if not (setting.amount_allocated > 0 and setting.risk_per_trade > 0
                and setting.max_daily_loss > 0 and setting.risk_per_trade <= setting.amount_allocated
                and setting.max_daily_loss <= setting.amount_allocated
                and Decimal(0) < setting.stop_limit_buffer_pct <= Decimal(20)):
            raise RiskBlocked("Strategy risk settings are incomplete or invalid")
        if setting.stop_mode != "broker_stop_limit" or setting.adverse_action != "emergency_exit":
            raise RiskBlocked("Live trading requires broker stop-limit plus emergency guardian exit")
        return Limits(setting.amount_allocated, setting.risk_per_trade,
                      setting.max_daily_loss, setting.stop_limit_buffer_pct,
                      setting.stop_mode, setting.adverse_action)


def realized_today(user_id: int, strategy_key: str, today: date) -> Decimal:
    with get_db() as db:
        return Decimal(db.scalar(select(func.coalesce(func.sum(StrategyTrade.gross_option_pnl), 0)).where(
            StrategyTrade.user_id == user_id,
            StrategyTrade.strategy_key == strategy_key,
            StrategyTrade.mode == "live",
            StrategyTrade.trading_date == today,
            StrategyTrade.status == "closed",
        )) or 0)


def guardian_fresh(user_id: int, strategy_key: str, *, max_age_seconds=20) -> bool:
    with get_db() as db:
        status = db.scalar(select(StrategyGuardianStatus).where(
            StrategyGuardianStatus.user_id == user_id,
            StrategyGuardianStatus.strategy_key == strategy_key,
        ))
        return bool(status and status.status == "healthy" and status.heartbeat_at
                    and status.heartbeat_at >= datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds))


def check_new_entry(user_id: int, strategy_key: str, today: date,
                    estimated_premium: Decimal, quantity: int) -> Limits:
    limits = load_limits(user_id, strategy_key)
    if not guardian_fresh(user_id, strategy_key):
        raise RiskBlocked("Independent strategy guardian is not healthy; new entry blocked")
    if estimated_premium <= 0 or quantity <= 0:
        raise RiskBlocked("Option quote or quantity is invalid")
    # The 10% quote cushion is a pre-trade gate, not a guaranteed fill cap.
    with get_db() as db:
        unresolved = db.scalar(select(func.count()).select_from(StrategyTrade).where(
            StrategyTrade.user_id == user_id,
            StrategyTrade.strategy_key == strategy_key,
            StrategyTrade.mode == "live",
            StrategyTrade.status.in_(["entry_pending", "exit_pending", "reconcile"]),
        ))
        if unresolved:
            raise RiskBlocked("Unresolved strategy order or position; new entry blocked")
        open_cost = Decimal(db.scalar(select(func.coalesce(func.sum(
            StrategyTrade.entry_premium * StrategyTrade.quantity), 0)).where(
                StrategyTrade.user_id == user_id,
                StrategyTrade.strategy_key == strategy_key,
                StrategyTrade.mode == "live",
                StrategyTrade.status == "open",
        )) or 0)
    if open_cost + estimated_premium * quantity * Decimal("1.10") > limits.amount_allocated:
        raise RiskBlocked("Estimated one-lot premium exceeds allocated capital")
    if realized_today(user_id, strategy_key, today) <= -limits.max_daily_loss:
        raise RiskBlocked("Daily strategy loss limit reached")
    return limits


def stop_limit_prices(entry_premium: Decimal, risk_rupees: Decimal, quantity: int,
                      tick_size: Decimal, buffer_pct=Decimal(3)) -> tuple[Decimal, Decimal]:
    """Sell SL trigger at rupee-loss boundary; limit below by configured %."""
    buffer_pct = Decimal(str(buffer_pct))
    if (entry_premium <= 0 or risk_rupees <= 0 or quantity <= 0 or tick_size <= 0
            or not Decimal(0) < buffer_pct <= Decimal(20)):
        raise RiskBlocked("Invalid option stop inputs")
    trigger = ((entry_premium - risk_rupees / quantity) / tick_size).to_integral_value(
        rounding=ROUND_CEILING) * tick_size
    limit = ((trigger * (Decimal(1) - buffer_pct / Decimal(100))) / tick_size).to_integral_value(
        rounding=ROUND_DOWN) * tick_size
    if trigger <= tick_size or limit <= 0 or limit >= trigger:
        raise RiskBlocked("Risk limit is too large for a valid protective option stop")
    return trigger, limit
