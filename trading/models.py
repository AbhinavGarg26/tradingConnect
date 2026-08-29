"""
SQLAlchemy models for the trading system.
Mirrors the PostgreSQL schema exactly — Rails owns DDL, Python owns data.
"""

from __future__ import annotations

import uuid
from datetime import datetime, date, timezone
from decimal import Decimal
from typing import Optional, List

from sqlalchemy import (
    BigInteger, Boolean, Date, DateTime, ForeignKey,
    Index, Numeric, String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from trading.exchange_link import ExchangeLink

from trading.database import Base


# ─────────────────────────────────────────────────────────────
# Mixins
# ─────────────────────────────────────────────────────────────

class TimestampMixin:
    """Adds created_at / updated_at to every model."""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class UUIDPrimaryKeyMixin:
    """UUID primary key — matches Rails gen_random_uuid()."""
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )


# ─────────────────────────────────────────────────────────────
# User
# ─────────────────────────────────────────────────────────────

class User(TimestampMixin, Base):
    __tablename__ = "users"

    id:         Mapped[int]            = mapped_column(BigInteger, primary_key=True)
    first_name: Mapped[str]            = mapped_column(String(255), nullable=False)
    last_name:                  Mapped[str] = mapped_column(String(255), nullable=False)
    email:                      Mapped[str]            = mapped_column(String(255), nullable=False, unique=True)

    # Relationships
    trades:         Mapped[List["Trade"]]        = relationship("Trade",        back_populates="user")
    market_configs:        Mapped[List["MarketConfig"]]       = relationship("MarketConfig",       back_populates="user")
    support_levels: Mapped[List["SupportLevel"]] = relationship("SupportLevel", back_populates="user")
    positions:      Mapped[List["Position"]]     = relationship("Position",     back_populates="user")
    exchange_link:  Mapped[Optional["ExchangeLink"]]   = relationship("ExchangeLink",  back_populates="user", uselist=False)

    def __repr__(self) -> str:
        return f"<User {self.email}>"

    def get_config(self, key: str, default=None):
        """Fetch a single config value by key."""
        for c in self.market_configs:
            if c.key == key:
                return c.typed_value
        return default

    @property
    def is_token_valid(self) -> bool:
        if not self.kite_access_token or not self.token_expires_at:
            return False
        return datetime.now(self.token_expires_at.tzinfo) < self.token_expires_at


# ─────────────────────────────────────────────────────────────
# Instrument
# ─────────────────────────────────────────────────────────────

class Instrument(TimestampMixin, Base):
    __tablename__ = "instruments"

    id:               Mapped[int]            = mapped_column(BigInteger, primary_key=True)
    symbol:           Mapped[str]            = mapped_column(String(50),  nullable=False)
    exchange:         Mapped[str]            = mapped_column(String(10),  nullable=False)  # NSE | BSE | NFO | MCX
    segment:          Mapped[str]            = mapped_column(String(10),  nullable=False)  # EQ | FO | FUT
    instrument_type:  Mapped[str]            = mapped_column(String(10),  nullable=False)  # EQ | CE | PE | FUT
    instrument_token: Mapped[int]            = mapped_column(BigInteger,  nullable=False, unique=True)
    lot_size:         Mapped[int]            = mapped_column(nullable=False, default=1)
    tick_size:        Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    expiry_date:      Mapped[Optional[date]]   = mapped_column(Date)
    strike_price:     Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    is_active:        Mapped[bool]           = mapped_column(Boolean, nullable=False, default=True)

    # Relationships
    trades:         Mapped[List["Trade"]]        = relationship("Trade",        back_populates="instrument")
    support_levels: Mapped[List["SupportLevel"]] = relationship("SupportLevel", back_populates="instrument")
    positions:      Mapped[List["Position"]]     = relationship("Position",     back_populates="instrument")

    __table_args__ = (
        Index("ix_instruments_symbol_exchange", "symbol", "exchange", "segment"),
        Index("ix_instruments_active", "is_active"),
    )

    def __repr__(self) -> str:
        return f"<Instrument {self.symbol} [{self.exchange}:{self.segment}]>"

    @property
    def is_options(self) -> bool:
        return self.instrument_type in ("CE", "PE")

    @property
    def is_futures(self) -> bool:
        return self.instrument_type == "FUT"

    @property
    def is_equity(self) -> bool:
        return self.instrument_type == "EQ"

    @property
    def display_name(self) -> str:
        if self.is_options and self.strike_price and self.expiry_date:
            return f"{self.symbol} {self.strike_price} {self.instrument_type} {self.expiry_date.strftime('%d%b%y').upper()}"
        if self.is_futures and self.expiry_date:
            return f"{self.symbol} FUT {self.expiry_date.strftime('%d%b%y').upper()}"
        return self.symbol


# ─────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────

class MarketConfig(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "market_configs"

    user_id:                   Mapped[int]              = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    key:         Mapped[str]            = mapped_column(String(100), nullable=False)
    value:       Mapped[str]            = mapped_column(String(500), nullable=False)
    data_type:   Mapped[str]            = mapped_column(String(20),  nullable=False, default="string")
    category:    Mapped[Optional[str]]  = mapped_column(String(50))
    description: Mapped[Optional[str]]  = mapped_column(Text)

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="market_configs")

    __table_args__ = (
        UniqueConstraint("user_id", "key", name="uq_market_configs_user_key"),
    )

    def __repr__(self) -> str:
        return f"<MarketConfig {self.key}={self.value}>"

    @property
    def typed_value(self):
        """Returns value cast to the correct Python type."""
        if self.data_type == "integer":
            return int(self.value)
        if self.data_type == "float":
            return float(self.value)
        if self.data_type == "boolean":
            return self.value.lower() in ("true", "1", "yes")
        return self.value  # string default

    # Default config keys used by the Python engine
    DEFAULTS = {
        "atr_period":          ("14",    "integer", "sl_strategy"),
        "atr_multiplier":      ("1.5",   "float",   "sl_strategy"),
        "trail_method":        ("swing", "string",  "sl_strategy"),
        "risk_per_trade":      ("2000",  "integer", "risk"),
        "max_open_trades":     ("5",     "integer", "risk"),
        "support_zone_buffer": ("0.3",   "float",   "sl_strategy"),
        "telegram_bot_token":  ("",      "string",  "alert"),
        "telegram_chat_id":    ("",      "string",  "alert"),
    }


# ─────────────────────────────────────────────────────────────
# SupportLevel
# ─────────────────────────────────────────────────────────────

class SupportLevel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "support_levels"

    instrument_id: Mapped[int]         = mapped_column(BigInteger, ForeignKey("instruments.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    price_level:   Mapped[Decimal]           = mapped_column(Numeric(12, 2), nullable=False)
    zone_upper:    Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    zone_lower:    Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    level_type:    Mapped[str]               = mapped_column(String(30), nullable=False)
    # support | resistance | demand_zone | supply_zone | pdl | pdh | vwap_anchor
    timeframe:     Mapped[Optional[str]]     = mapped_column(String(10))
    is_active:     Mapped[bool]              = mapped_column(Boolean, nullable=False, default=True)
    valid_from:    Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    valid_until:   Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    notes:         Mapped[Optional[str]]     = mapped_column(Text)

    # Relationships
    instrument: Mapped["Instrument"] = relationship("Instrument", back_populates="support_levels")
    user:       Mapped["User"]       = relationship("User",       back_populates="support_levels")

    __table_args__ = (
        Index("ix_support_levels_active", "instrument_id", "is_active"),
        Index("ix_support_levels_type",   "instrument_id", "level_type"),
    )

    def __repr__(self) -> str:
        return f"<SupportLevel {self.level_type} @ {self.price_level}>"

    def is_price_near(self, price: Decimal, buffer_pct: float = 0.3) -> bool:
        """Returns True if price is within buffer_pct% of this level."""
        buffer = self.price_level * Decimal(str(buffer_pct / 100))
        lower = self.price_level - buffer
        upper = self.price_level + buffer
        return lower <= price <= upper

    def is_price_rejecting(self, current: Decimal, prev_low: Decimal) -> bool:
        """
        Simple rejection check:
        price touched the zone then closed back above it.
        Call with current candle close and candle low.
        """
        if self.zone_lower and self.zone_upper:
            touched = prev_low <= self.zone_upper
            bounced = current > self.price_level
            return touched and bounced
        return self.is_price_near(prev_low) and current > self.price_level

    @property
    def is_currently_valid(self) -> bool:
        now = datetime.utcnow()
        if not self.is_active:
            return False
        if self.valid_from and now < self.valid_from.replace(tzinfo=None):
            return False
        if self.valid_until and now > self.valid_until.replace(tzinfo=None):
            return False
        return True


# ─────────────────────────────────────────────────────────────
# Trade
# ─────────────────────────────────────────────────────────────

class Trade(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "trades"

    instrument_id:   Mapped[int]         = mapped_column(BigInteger, ForeignKey("instruments.id"), nullable=False)
    user_id:                   Mapped[int]              = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)

    # Direction & type
    direction:       Mapped[str]               = mapped_column(String(5),  nullable=False)   # BUY | SELL
    trade_type:      Mapped[str]               = mapped_column(String(10), nullable=False)   # EQUITY | OPTIONS | FUTURES
    product:         Mapped[str]               = mapped_column(String(10), nullable=False)   # CNC | MIS | NRML

    # Entry
    entry_price:     Mapped[Decimal]           = mapped_column(Numeric(12, 2), nullable=False)
    quantity:        Mapped[Decimal]           = mapped_column(Numeric(12, 2), nullable=False)
    entry_value:     Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2))

    # Stop loss
    initial_sl:      Mapped[Decimal]           = mapped_column(Numeric(12, 2), nullable=False)
    current_sl:      Mapped[Decimal]           = mapped_column(Numeric(12, 2), nullable=False)
    sl_distance_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 4))
    sl_method:       Mapped[Optional[str]]     = mapped_column(String(20))

    # Target
    target_price:    Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    risk_amount:     Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    reward_ratio:    Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2))

    # Exit
    exit_price:      Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    exit_value:      Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2))
    exit_reason:     Mapped[Optional[str]]     = mapped_column(String(30))
    pnl:             Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    pnl_pct:         Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4))
    charges:         Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))

    # Status
    status:          Mapped[str]               = mapped_column(String(15), nullable=False, default="open")

    # Timestamps
    entered_at:      Mapped[datetime]          = mapped_column(DateTime(timezone=True), nullable=False)
    exited_at:       Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # Relationships
    instrument:   Mapped["Instrument"]       = relationship("Instrument",   back_populates="trades")
    user:         Mapped["User"]             = relationship("User",         back_populates="trades")
    position:     Mapped[Optional["Position"]] = relationship("Position",   back_populates="trade", uselist=False)
    stop_loss_histories: Mapped[List["StopLossHistory"]]  = relationship("StopLossHistory",    back_populates="trade",  order_by="StopLossHistory.adjusted_at")
    order_events: Mapped[List["OrderEvent"]] = relationship("OrderEvent",   back_populates="trade",  order_by="OrderEvent.placed_at")

    __table_args__ = (
        Index("ix_trades_status",            "status"),
        Index("ix_trades_instrument_status", "instrument_id", "status"),
        Index("ix_trades_user_status",       "user_id", "status"),
        Index("ix_trades_entered_at",        "entered_at"),
        Index("ix_trades_trade_type",        "trade_type"),
    )

    def __repr__(self) -> str:
        return f"<Trade {self.direction} {self.instrument_id} @ {self.entry_price} [{self.status}]>"

    # ── Status helpers ──────────────────────────────────────

    @property
    def is_open(self) -> bool:
        return self.status == "open"

    @property
    def is_closed(self) -> bool:
        return self.status == "closed"

    # ── SL helpers ──────────────────────────────────────────

    @property
    def sl_trail_count(self) -> int:
        return len(self.stop_loss_histories)

    @property
    def sl_moved_pct(self) -> Optional[Decimal]:
        """How far SL has moved from initial as a % of entry price."""
        if not self.entry_price:
            return None
        moved = abs(self.current_sl - self.initial_sl)
        return (moved / self.entry_price * 100).quantize(Decimal("0.01"))

    def update_sl(self, new_sl: Decimal, method: str, reason: str,
                  price_at_time: Decimal, atr_value: Optional[Decimal] = None,
                  r_multiple: Optional[Decimal] = None) -> "StopLossHistory":
        """
        Trail the stop loss. Creates an StopLossHistory record and updates current_sl.
        Call db.commit() after this to persist.
        """
        if self.direction == "BUY" and new_sl <= self.current_sl:
            raise ValueError(f"New SL {new_sl} must be higher than current {self.current_sl} for BUY")
        if self.direction == "SELL" and new_sl >= self.current_sl:
            raise ValueError(f"New SL {new_sl} must be lower than current {self.current_sl} for SELL")

        history = StopLossHistory(
            trade_id=self.id,
            old_sl=self.current_sl,
            new_sl=new_sl,
            price_at_time=price_at_time,
            method=method,
            trigger_reason=reason,
            atr_value=atr_value,
            r_multiple=r_multiple,
            adjusted_at=datetime.utcnow(),
        )
        self.current_sl = new_sl
        self.stop_loss_histories.append(history)
        return history

    def close(self, exit_price: Decimal, exit_reason: str, charges: Decimal = Decimal("0")) -> None:
        """Mark trade as closed and compute P&L."""
        self.exit_price = exit_price
        self.exit_reason = exit_reason
        self.exited_at = datetime.utcnow()
        self.status = "closed"
        self.exit_value = exit_price * self.quantity

        if self.direction == "BUY":
            raw_pnl = (exit_price - self.entry_price) * self.quantity
        else:
            raw_pnl = (self.entry_price - exit_price) * self.quantity

        self.charges = charges
        self.pnl = raw_pnl - charges
        if self.entry_value:
            self.pnl_pct = (self.pnl / self.entry_value * 100).quantize(Decimal("0.0001"))

    def is_sl_hit(self, ltp: Decimal) -> bool:
        if self.direction == "BUY":
            return ltp <= self.current_sl
        return ltp >= self.current_sl

    def is_target_hit(self, ltp: Decimal) -> bool:
        if not self.target_price:
            return False
        if self.direction == "BUY":
            return ltp >= self.target_price
        return ltp <= self.target_price

    def r_multiple_achieved(self, ltp: Decimal) -> Optional[Decimal]:
        """Current R-multiple (how many times risk is the current profit)."""
        if not self.risk_amount or self.risk_amount == 0:
            return None
        if self.direction == "BUY":
            profit = (ltp - self.entry_price) * self.quantity
        else:
            profit = (self.entry_price - ltp) * self.quantity
        return (profit / self.risk_amount).quantize(Decimal("0.01"))


# ─────────────────────────────────────────────────────────────
# Position
# ─────────────────────────────────────────────────────────────

class Position(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "positions"

    instrument_id:    Mapped[int]               = mapped_column(BigInteger, ForeignKey("instruments.id"), nullable=False)
    trade_id:         Mapped[uuid.UUID]         = mapped_column(UUID(as_uuid=True), ForeignKey("trades.id"),      nullable=False, unique=True)
    user_id:                   Mapped[int]              = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)

    avg_price:        Mapped[Decimal]           = mapped_column(Numeric(12, 2), nullable=False)
    quantity:         Mapped[Decimal]           = mapped_column(Numeric(12, 2), nullable=False)
    last_price:       Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    unrealised_pnl:   Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    realised_pnl:     Mapped[Decimal]           = mapped_column(Numeric(12, 2), default=Decimal("0"))
    day_buy_quantity: Mapped[Decimal]           = mapped_column(Numeric(12, 2), default=Decimal("0"))
    day_sell_quantity:Mapped[Decimal]           = mapped_column(Numeric(12, 2), default=Decimal("0"))
    product:          Mapped[Optional[str]]     = mapped_column(String(10))
    status:           Mapped[str]               = mapped_column(String(15), nullable=False, default="open")
    opened_at:        Mapped[datetime]          = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at:        Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # Relationships
    instrument: Mapped["Instrument"] = relationship("Instrument", back_populates="positions")
    trade:      Mapped["Trade"]      = relationship("Trade",      back_populates="position")
    user:       Mapped["User"]       = relationship("User",       back_populates="positions")

    __table_args__ = (
        Index("ix_positions_instrument_status", "instrument_id", "status"),
        Index("ix_positions_user_status",       "user_id", "status"),
    )

    def __repr__(self) -> str:
        return f"<Position {self.instrument_id} qty={self.quantity} [{self.status}]>"

    def sync_from_kite(self, kite_position: dict) -> None:
        """Update position fields from a Kite positions API response dict."""
        self.last_price       = Decimal(str(kite_position.get("last_price", 0)))
        self.unrealised_pnl   = Decimal(str(kite_position.get("unrealised", 0)))
        self.realised_pnl     = Decimal(str(kite_position.get("realised", 0)))
        self.day_buy_quantity  = Decimal(str(kite_position.get("day_buy_quantity", 0)))
        self.day_sell_quantity = Decimal(str(kite_position.get("day_sell_quantity", 0)))
        self.quantity          = Decimal(str(kite_position.get("quantity", 0)))
        self.avg_price         = Decimal(str(kite_position.get("average_price", 0)))


# ─────────────────────────────────────────────────────────────
# StopLossHistory
# ─────────────────────────────────────────────────────────────

class StopLossHistory(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "stop_loss_histories"

    trade_id:       Mapped[uuid.UUID]         = mapped_column(UUID(as_uuid=True), ForeignKey("trades.id"), nullable=False)
    old_sl:         Mapped[Decimal]           = mapped_column(Numeric(12, 2), nullable=False)
    new_sl:         Mapped[Decimal]           = mapped_column(Numeric(12, 2), nullable=False)
    price_at_time:  Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    method:         Mapped[str]               = mapped_column(String(20), nullable=False)
    trigger_reason: Mapped[Optional[str]]     = mapped_column(String(40))
    atr_value:      Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4))
    r_multiple:     Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2))
    adjusted_at:    Mapped[datetime]          = mapped_column(DateTime(timezone=True), nullable=False)

    # Relationships
    trade: Mapped["Trade"] = relationship("Trade", back_populates="stop_loss_histories")

    __table_args__ = (
        Index("ix_stop_loss_histories_trade",       "trade_id"),
        Index("ix_stop_loss_histories_adjusted_at", "adjusted_at"),
    )

    def __repr__(self) -> str:
        return f"<StopLossHistory {self.old_sl} → {self.new_sl} [{self.method}]>"

    @property
    def sl_improvement(self) -> Decimal:
        """Positive = SL moved in favour of the trade."""
        return abs(self.new_sl - self.old_sl)


# ─────────────────────────────────────────────────────────────
# OrderEvent
# ─────────────────────────────────────────────────────────────

class OrderEvent(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "order_events"

    trade_id:         Mapped[uuid.UUID]         = mapped_column(UUID(as_uuid=True), ForeignKey("trades.id"), nullable=False)
    order_id:    Mapped[str]               = mapped_column(String(50), nullable=False)
    parent_order_id:  Mapped[Optional[str]]     = mapped_column(String(50))
    order_type:       Mapped[str]               = mapped_column(String(10), nullable=False)  # MARKET | LIMIT | SL | SL-M
    transaction_type: Mapped[str]               = mapped_column(String(5),  nullable=False)  # BUY | SELL
    variety:          Mapped[str]               = mapped_column(String(15), nullable=False)  # regular | amo | co | iceberg
    status:           Mapped[str]               = mapped_column(String(15), nullable=False)  # OPEN | COMPLETE | CANCELLED | REJECTED
    status_message:   Mapped[Optional[str]]     = mapped_column(String(255))
    price:            Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    trigger_price:    Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    quantity:         Mapped[Decimal]           = mapped_column(Numeric(12, 2), nullable=False)
    filled_quantity:  Mapped[Decimal]           = mapped_column(Numeric(12, 2), default=Decimal("0"))
    average_price:    Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    exchange_order_id:Mapped[Optional[str]]     = mapped_column(String(50))
    placed_at:        Mapped[datetime]          = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at:       Mapped[datetime]          = mapped_column(DateTime(timezone=True), nullable=False)
    created_at:       Mapped[datetime]          = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Relationships
    trade: Mapped["Trade"] = relationship("Trade", back_populates="order_events")

    __table_args__ = (
        Index("ix_order_events_kite_id",      "order_id"),
        Index("ix_order_events_trade_status", "trade_id", "status"),
        Index("ix_order_events_placed_at",    "placed_at"),
    )

    def __repr__(self) -> str:
        return f"<OrderEvent {self.order_id} {self.order_type} [{self.status}]>"

    @property
    def is_complete(self) -> bool:
        return self.status == "COMPLETE"

    @property
    def is_rejected(self) -> bool:
        return self.status == "REJECTED"

    @property
    def is_partial(self) -> bool:
        return self.status == "OPEN" and self.filled_quantity > 0

    @classmethod
    def from_kite_order(cls, trade_id: uuid.UUID, order: dict) -> "OrderEvent":
        """Build an OrderEvent directly from Kite order update dict."""
        from datetime import timezone
        return cls(
            trade_id=trade_id,
            order_id=order["order_id"],
            parent_order_id=order.get("parent_order_id"),
            order_type=order["order_type"],
            transaction_type=order["transaction_type"],
            variety=order["variety"],
            status=order["status"],
            status_message=order.get("status_message"),
            price=Decimal(str(order.get("price", 0))) or None,
            trigger_price=Decimal(str(order.get("trigger_price", 0))) or None,
            quantity=Decimal(str(order["quantity"])),
            filled_quantity=Decimal(str(order.get("filled_quantity", 0))),
            average_price=Decimal(str(order.get("average_price", 0))) or None,
            exchange_order_id=order.get("exchange_order_id"),
            placed_at=order["order_timestamp"],
            updated_at=order.get("exchange_update_timestamp") or order["order_timestamp"],
        )