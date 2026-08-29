"""
Repository layer — all DB queries live here.
The trade engine imports these instead of writing raw SQLAlchemy queries inline.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Optional, List

from sqlalchemy import select, and_
from sqlalchemy.orm import Session, joinedload

from trading.models import (
    User, Instrument, Trade, Position,
    SupportLevel, StopLossHistory, OrderEvent, MarketConfig,
)


# ─────────────────────────────────────────────────────────────
# UserRepo
# ─────────────────────────────────────────────────────────────

class UserRepo:

    @staticmethod
    def get_active(db: Session) -> Optional[User]:
        """Fetch the single active user with market_configs eager-loaded."""
        return db.scalar(
            select(User)
            .order_by(User.created_at.asc())
            .options(joinedload(User.market_configs))
            .limit(1)
        )

    @staticmethod
    def update_token(db: Session, user_id: int, token: str, expires_at) -> None:
        user = db.get(User, user_id)
        if user:
            user.kite_access_token = token
            user.token_expires_at = expires_at


# ─────────────────────────────────────────────────────────────
# InstrumentRepo
# ─────────────────────────────────────────────────────────────

class InstrumentRepo:

    @staticmethod
    def get_by_token(db: Session, token: int) -> Optional[Instrument]:
        return db.scalar(select(Instrument).where(Instrument.instrument_token == token))

    @staticmethod
    def get_by_symbol(db: Session, symbol: str, exchange: str = "NSE") -> Optional[Instrument]:
        return db.scalar(
            select(Instrument).where(
                and_(Instrument.symbol == symbol, Instrument.exchange == exchange)
            )
        )

    @staticmethod
    def get_active_tokens(db: Session) -> List[int]:
        """Returns all instrument_tokens for active instruments — used for WebSocket subscribe."""
        rows = db.execute(
            select(Instrument.instrument_token).where(Instrument.is_active == True)
        ).scalars().all()
        return list(rows)


# ─────────────────────────────────────────────────────────────
# TradeRepo
# ─────────────────────────────────────────────────────────────

class TradeRepo:

    @staticmethod
    def get_open(db: Session) -> List[Trade]:
        """All open trades with instrument + stop_loss_histories eager-loaded."""
        return db.scalars(
            select(Trade)
            .where(Trade.status == "open")
            .options(
                joinedload(Trade.instrument),
                joinedload(Trade.stop_loss_histories),
                joinedload(Trade.order_events),
            )
        ).unique().all()

    @staticmethod
    def get_open_by_instrument(db: Session, instrument_id: uuid.UUID) -> List[Trade]:
        return db.scalars(
            select(Trade).where(
                and_(Trade.instrument_id == instrument_id, Trade.status == "open")
            ).options(joinedload(Trade.stop_loss_histories))
        ).unique().all()

    @staticmethod
    def get_by_id(db: Session, trade_id: uuid.UUID) -> Optional[Trade]:
        return db.scalar(
            select(Trade)
            .where(Trade.id == trade_id)
            .options(
                joinedload(Trade.instrument),
                joinedload(Trade.stop_loss_histories),
                joinedload(Trade.order_events),
                joinedload(Trade.position),
            )
        )

    @staticmethod
    def count_open(db: Session) -> int:
        from sqlalchemy import func
        return db.scalar(
            select(func.count()).select_from(Trade).where(Trade.status == "open")
        ) or 0

    @staticmethod
    def count_by_direction(db: Session, direction: str) -> int:
        from sqlalchemy import func
        return db.scalar(
            select(func.count()).select_from(Trade).where(Trade.direction == direction)
        ) or 0

    @staticmethod
    def get_dashboard_summary(db: Session) -> dict:
        """
        Single-query summary for the Rails dashboard.
        Returns dict consumed by the Rails API endpoint or ActionCable broadcast.
        """
        from sqlalchemy import func, case
        row = db.execute(
            select(
                func.count().label("total_trades"),
                func.sum(case((Trade.status == "open",  1), else_=0)).label("active_trades"),
                func.sum(case((Trade.direction == "BUY",  1), else_=0)).label("total_buys"),
                func.sum(case((Trade.direction == "SELL", 1), else_=0)).label("total_sells"),
                func.sum(case((Trade.status == "closed", Trade.pnl), else_=0)).label("total_pnl"),
                func.sum(case((and_(Trade.status == "closed", Trade.pnl > 0), 1), else_=0)).label("winning_trades"),
                func.sum(case((and_(Trade.status == "closed", Trade.pnl < 0), 1), else_=0)).label("losing_trades"),
            ).select_from(Trade)
        ).one()

        closed = (row.total_trades or 0) - (row.active_trades or 0)
        win_rate = (
            round(row.winning_trades / closed * 100, 1)
            if closed and row.winning_trades else 0
        )
        return {
            "total_trades":   row.total_trades   or 0,
            "active_trades":  row.active_trades  or 0,
            "total_buys":     row.total_buys     or 0,
            "total_sells":    row.total_sells    or 0,
            "total_pnl":      float(row.total_pnl or 0),
            "winning_trades": row.winning_trades or 0,
            "losing_trades":  row.losing_trades  or 0,
            "win_rate_pct":   win_rate,
        }


# ─────────────────────────────────────────────────────────────
# SupportLevelRepo
# ─────────────────────────────────────────────────────────────

class SupportLevelRepo:

    @staticmethod
    def get_active_for_instrument(db: Session, instrument_id: uuid.UUID) -> List[SupportLevel]:
        """Fetch all currently active support levels for an instrument."""
        from datetime import datetime
        now = datetime.utcnow()
        return db.scalars(
            select(SupportLevel).where(
                and_(
                    SupportLevel.instrument_id == instrument_id,
                    SupportLevel.is_active == True,
                    (SupportLevel.valid_until == None) | (SupportLevel.valid_until > now),
                )
            )
        ).all()

    @staticmethod
    def find_near_price(
        db: Session,
        instrument_id: uuid.UUID,
        price: Decimal,
        buffer_pct: float = 0.3,
    ) -> List[SupportLevel]:
        """Returns levels within buffer_pct% of the given price."""
        levels = SupportLevelRepo.get_active_for_instrument(db, instrument_id)
        return [lvl for lvl in levels if lvl.is_price_near(price, buffer_pct)]


# ─────────────────────────────────────────────────────────────
# OrderEventRepo
# ─────────────────────────────────────────────────────────────

class OrderEventRepo:

    @staticmethod
    def upsert_from_kite(db: Session, trade_id: uuid.UUID, kite_order: dict) -> OrderEvent:
        """
        Insert a new OrderEvent or update status if kite_order_id already exists.
        """
        existing = db.scalar(
            select(OrderEvent).where(
                OrderEvent.kite_order_id == kite_order["order_id"]
            )
        )
        if existing:
            existing.status = kite_order["status"]
            existing.status_message = kite_order.get("status_message")
            existing.filled_quantity = Decimal(str(kite_order.get("filled_quantity", 0)))
            existing.average_price = Decimal(str(kite_order.get("average_price", 0))) or None
            existing.updated_at = kite_order.get("exchange_update_timestamp") or kite_order["order_timestamp"]
            return existing

        event = OrderEvent.from_kite_order(trade_id, kite_order)
        db.add(event)
        return event


# ─────────────────────────────────────────────────────────────
# MarketConfigRepo
# ─────────────────────────────────────────────────────────────

class MarketConfigRepo:

    @staticmethod
    def get(db: Session, user_id: int, key: str, default=None):
        """Get a single typed config value."""
        config = db.scalar(
            select(MarketConfig).where(
                and_(MarketConfig.user_id == user_id, MarketConfig.key == key)
            )
        )
        return config.typed_value if config else default

    @staticmethod
    def get_all(db: Session, user_id: int) -> dict:
        """Return all market_configs as a typed dict."""
        rows = db.scalars(
            select(MarketConfig).where(MarketConfig.user_id == user_id)
        ).all()
        return {c.key: c.typed_value for c in rows}

    @staticmethod
    def seed_defaults(db: Session, user_id: int) -> None:
        """Insert default config rows for a new user if they don't exist."""
        existing_keys = {
            row[0] for row in db.execute(
                select(MarketConfig.key).where(MarketConfig.user_id == user_id)
            ).all()
        }
        for key, (value, data_type, category) in MarketConfig.DEFAULTS.items():
            if key not in existing_keys:
                db.add(MarketConfig(
                    user_id=user_id,
                    key=key,
                    value=value,
                    data_type=data_type,
                    category=category,
                ))