"""Durable journal for the NIFTY strategy. Rails owns these tables' migrations."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from strategies.nifty_15m_breakout import STRATEGY_VERSION
from trading.database import Base, get_db


STRATEGY_KEY = "nifty_15m_breakout"


def signal_time(decision):
    """Kite historical candles are labelled by start; the decision is known at close."""
    return decision.time + timedelta(minutes=15)


class StrategyRun(Base):
    __tablename__ = "strategy_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    strategy_key: Mapped[str] = mapped_column(String, nullable=False)
    strategy_version: Mapped[str] = mapped_column(String, nullable=False)
    mode: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)
    configuration: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class StrategyTrade(Base):
    __tablename__ = "strategy_trades"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    strategy_run_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("strategy_runs.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    strategy_key: Mapped[str] = mapped_column(String, nullable=False)
    strategy_version: Mapped[str] = mapped_column(String, nullable=False)
    mode: Mapped[str] = mapped_column(String, nullable=False)
    trading_date: Mapped[date] = mapped_column(Date, nullable=False)
    trade_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    side: Mapped[str] = mapped_column(String, nullable=False)
    option_symbol: Mapped[str] = mapped_column(String, nullable=False)
    option_expiry: Mapped[date] = mapped_column(Date, nullable=False)
    option_strike: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    lot_count: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_signal_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exit_signal_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    entry_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    entry_reason: Mapped[str] = mapped_column(String, nullable=False)
    exit_reason: Mapped[str | None] = mapped_column(String)
    entry_index: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    exit_index: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    reference_high: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    reference_low: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    prior_entry_high: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    prior_entry_low: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    initial_stop: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    current_stop: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    target: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    entry_order_id: Mapped[str | None] = mapped_column(String)
    exit_order_id: Mapped[str | None] = mapped_column(String)
    entry_premium: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    exit_premium: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    gross_option_pnl: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    index_points: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class StrategyEvent(Base):
    __tablename__ = "strategy_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    strategy_run_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("strategy_runs.id"), nullable=False)
    strategy_trade_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("strategy_trades.id"))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str] = mapped_column(String, nullable=False)
    index_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    stop: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    target: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    details: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class StrategyRecorder:
    def __init__(self, user_id: int, *, live: bool, max_risk_points, max_trades_per_day=4):
        self.user_id = user_id
        self.mode = "live" if live else "paper"
        self.trade_id = None
        now = datetime.now(timezone.utc)
        with get_db() as db:
            record = StrategyRun(
                user_id=user_id, strategy_key=STRATEGY_KEY,
                strategy_version=STRATEGY_VERSION, mode=self.mode, status="running",
                started_at=now, heartbeat_at=now,
                configuration={"max_risk_points": str(max_risk_points),
                               "max_trades_per_day": max_trades_per_day,
                               "lot_count": 1, "stop_step_points": 10},
            )
            db.add(record)
            db.flush()
            self.run_id = record.id

    def event(self, decision, *, trade_id=None, details=None):
        with get_db() as db:
            db.add(StrategyEvent(
                strategy_run_id=self.run_id, strategy_trade_id=trade_id,
                occurred_at=signal_time(decision), action=decision.action,
                reason=decision.reason or "", index_price=decision.index_price,
                stop=decision.stop, target=decision.target, details=details or {},
            ))
            run = db.get(StrategyRun, self.run_id)
            run.heartbeat_at = datetime.now(timezone.utc)
            run.updated_at = run.heartbeat_at

    def heartbeat(self):
        with get_db() as db:
            run = db.get(StrategyRun, self.run_id)
            run.heartbeat_at = datetime.now(timezone.utc)
            run.updated_at = run.heartbeat_at

    def begin_trade(self, decision, contract, quantity, trade_number):
        with get_db() as db:
            count = db.scalar(select(func.count()).select_from(StrategyTrade).where(
                StrategyTrade.user_id == self.user_id,
                StrategyTrade.strategy_key == STRATEGY_KEY,
                StrategyTrade.mode == self.mode,
                StrategyTrade.trading_date == decision.time.date(),
            ))
            if count >= 4:
                raise RuntimeError("Four strategy trades are already recorded for today; no further entry")
            trade = StrategyTrade(
                strategy_run_id=self.run_id, user_id=self.user_id,
                strategy_key=STRATEGY_KEY, strategy_version=STRATEGY_VERSION,
                mode=self.mode, trading_date=decision.time.date(),
                trade_number=trade_number, status="entry_pending" if self.mode == "live" else "open",
                side=decision.action[-2:], option_symbol=contract["tradingsymbol"],
                option_expiry=contract["expiry"], option_strike=contract["strike"],
                lot_count=1, quantity=quantity,
                entry_signal_time=signal_time(decision), entry_reason=decision.reason,
                entry_index=decision.index_price, initial_stop=decision.stop,
                reference_high=decision.reference_high,
                reference_low=decision.reference_low,
                prior_entry_high=decision.prior_entry_high,
                prior_entry_low=decision.prior_entry_low,
                current_stop=decision.stop, target=decision.target,
            )
            db.add(trade)
            db.flush()
            self.trade_id = trade.id
            db.add(StrategyEvent(
                strategy_run_id=self.run_id, strategy_trade_id=trade.id,
                occurred_at=signal_time(decision), action=decision.action, reason=decision.reason,
                index_price=decision.index_price, stop=decision.stop,
                target=decision.target,
                details={"option_symbol": contract["tradingsymbol"], "quantity": quantity,
                         "lot_count": 1,
                         "reference_high": str(decision.reference_high),
                         "reference_low": str(decision.reference_low),
                         "prior_entry_high": str(decision.prior_entry_high) if decision.prior_entry_high else None,
                         "prior_entry_low": str(decision.prior_entry_low) if decision.prior_entry_low else None},
            ))
        return self.trade_id

    def entry_filled(self, order_id, premium):
        with get_db() as db:
            trade = db.get(StrategyTrade, self.trade_id)
            trade.entry_order_id = str(order_id)
            trade.entry_premium = premium
            trade.entry_confirmed_at = datetime.now(timezone.utc)
            trade.status = "open"
            trade.updated_at = datetime.now(timezone.utc)

    def order_submitted(self, order_id, *, exit_order):
        with get_db() as db:
            trade = db.get(StrategyTrade, self.trade_id)
            if exit_order:
                trade.exit_order_id = str(order_id)
            else:
                trade.entry_order_id = str(order_id)
            trade.updated_at = datetime.now(timezone.utc)

    def trail_stop(self, decision):
        with get_db() as db:
            trade = db.get(StrategyTrade, self.trade_id)
            trade.current_stop = decision.stop
            trade.updated_at = datetime.now(timezone.utc)
        self.event(decision, trade_id=self.trade_id)

    def exit_intent(self, decision):
        with get_db() as db:
            trade = db.get(StrategyTrade, self.trade_id)
            trade.exit_signal_time = signal_time(decision)
            trade.exit_reason = decision.reason
            trade.exit_index = decision.index_price
            trade.status = "exit_pending" if self.mode == "live" else "closed"
            direction = Decimal(1) if trade.side == "CE" else Decimal(-1)
            trade.index_points = direction * (decision.index_price - trade.entry_index)
            trade.updated_at = datetime.now(timezone.utc)
        self.event(decision, trade_id=self.trade_id)

    def exit_filled(self, order_id, premium):
        with get_db() as db:
            trade = db.get(StrategyTrade, self.trade_id)
            trade.exit_order_id = str(order_id)
            trade.exit_premium = premium
            trade.exit_confirmed_at = datetime.now(timezone.utc)
            trade.gross_option_pnl = (premium - trade.entry_premium) * trade.quantity
            trade.status = "closed"
            trade.updated_at = datetime.now(timezone.utc)
        self.trade_id = None

    def paper_exit_complete(self):
        self.trade_id = None

    def mark_uncertain(self, message):
        if self.trade_id is None:
            return
        with get_db() as db:
            trade = db.get(StrategyTrade, self.trade_id)
            trade.status = "reconcile"
            trade.error_message = message[:2000]
            trade.updated_at = datetime.now(timezone.utc)

    def mark_pending_uncertain(self, message):
        if self.trade_id is None:
            return
        with get_db() as db:
            trade = db.get(StrategyTrade, self.trade_id)
            if trade.status in {"entry_pending", "exit_pending"}:
                trade.status = "reconcile"
                trade.error_message = message[:2000]
                trade.updated_at = datetime.now(timezone.utc)

    def finish(self, status, error_message=None):
        with get_db() as db:
            run = db.get(StrategyRun, self.run_id)
            run.status = status
            run.error_message = error_message[:2000] if error_message else None
            run.ended_at = datetime.now(timezone.utc)
            run.heartbeat_at = run.ended_at
            run.updated_at = run.ended_at
