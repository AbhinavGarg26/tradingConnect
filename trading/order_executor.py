"""
order_executor.py — executes OrderSignals on Kite.

Responsibilities:
  - EXIT signal       → place LIMIT exit order at current SL price
  - MODIFY_SL signal  → modify existing SL-M order on Kite
  - Retry once on rejection
  - Telegram alert on every rejection
  - Halt trading after 3 consecutive rejections
  - Write every order attempt to order_events table

Order flow per signal:
  handle(signal)
      │
      ├── EXIT        → _place_exit_order()   → LIMIT sell/buy at SL or support level
      └── MODIFY_SL   → _modify_sl_order()    → modify_order() on existing SL-M order
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from kiteconnect import KiteConnect
from kiteconnect.exceptions import (
    InputException,
    NetworkException,
    OrderException,
    TokenException,
)

from trading.alerts import Alerter
from trading.database import get_db
from trading.exchange_link import ExchangeLinkRepo
from trading.models import OrderEvent, Trade
from market.kite_orders import place_protected_market_order
from trading.repositories import MarketConfigRepo, TradeRepo
from trading.strategy_engine import OrderSignal, SignalType

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────

MAX_CONSECUTIVE_REJECTIONS = 3
RETRY_DELAY_SECONDS        = 1.0   # wait before retry attempt


# ─────────────────────────────────────────────────────────────
# ExecutorState — tracks consecutive rejections
# ─────────────────────────────────────────────────────────────

@dataclass
class ExecutorState:
    consecutive_rejections: int  = 0
    is_halted:              bool = False
    halt_reason:            str  = ""

    def record_rejection(self) -> None:
        self.consecutive_rejections += 1

    def record_success(self) -> None:
        self.consecutive_rejections = 0

    def should_halt(self) -> bool:
        return self.consecutive_rejections >= MAX_CONSECUTIVE_REJECTIONS

    def halt(self, reason: str) -> None:
        self.is_halted  = True
        self.halt_reason = reason
        logger.critical("OrderExecutor HALTED — %s", reason)

    def resume(self) -> None:
        self.is_halted              = False
        self.halt_reason            = ""
        self.consecutive_rejections = 0
        logger.info("OrderExecutor resumed")


# ─────────────────────────────────────────────────────────────
# OrderExecutor
# ─────────────────────────────────────────────────────────────

class OrderExecutor:
    """
    Stateful executor — one instance per session.

    Usage in main.py:
        executor = OrderExecutor(user_id=user_id)
        strategy.on_signal(executor.handle)
    """

    def __init__(self, user_id: int):
        self.user_id  = user_id
        self._state   = ExecutorState()
        self._alerter: Optional[Alerter] = None
        self._initialised = False

    # ── Public API ────────────────────────────────────────────

    def handle(self, signal: OrderSignal) -> None:
        """
        Entry point — receives every OrderSignal from StrategyEngine.
        Called in the WebSocket callback thread — must not block long.
        """
        if not self._initialised:
            self._load_config()

        if self._state.is_halted:
            logger.warning(
                "OrderExecutor halted — ignoring signal %s trade_id=%s",
                signal.signal_type, signal.trade.id,
            )
            return

        if signal.signal_type == SignalType.EXIT:
            self._handle_exit(signal)

        elif signal.signal_type == SignalType.MODIFY_SL:
            self._handle_modify_sl(signal)

    def resume(self) -> None:
        """Manually resume after a halt. Call from Rails admin or CLI."""
        self._state.resume()
        self._alerter.send("✅ <b>Order executor resumed</b> — trading active")

    def is_halted(self) -> bool:
        return self._state.is_halted

    # ── EXIT handler ──────────────────────────────────────────

    def _handle_exit(self, signal: OrderSignal) -> None:
        trade = signal.trade
        logger.info(
            "EXIT signal — trade_id=%s symbol=%s reason=%s",
            trade.id, trade.instrument.symbol, signal.exit_reason,
        )

        # Exit price: use current SL for sl_hit, last candle close for support_rejection
        if signal.exit_reason == "sl_hit":
            limit_price = trade.current_sl
        elif signal.exit_reason == "support_rejection":
            limit_price = Decimal(str(signal.candle.close)) if signal.candle else trade.current_sl
        else:
            limit_price = Decimal(str(signal.candle.close)) if signal.candle else trade.current_sl

        # Exit direction is opposite to trade direction
        transaction_type = (
            KiteConnect.TRANSACTION_TYPE_SELL
            if trade.direction == "BUY"
            else KiteConnect.TRANSACTION_TYPE_BUY
        )

        self._place_order_with_retry(
            trade=trade,
            transaction_type=transaction_type,
            order_type=KiteConnect.ORDER_TYPE_LIMIT,
            price=limit_price,
            quantity=int(trade.quantity),
            variety=KiteConnect.VARIETY_REGULAR,
            tag=f"EXIT_{signal.exit_reason[:10]}",
            exit_reason=signal.exit_reason,
        )

    # ── MODIFY_SL handler ─────────────────────────────────────

    def _handle_modify_sl(self, signal: OrderSignal) -> None:
        trade   = signal.trade
        new_sl  = signal.new_sl

        logger.info(
            "MODIFY_SL signal — trade_id=%s symbol=%s new_sl=%.2f",
            trade.id, trade.instrument.symbol, new_sl,
        )

        # Find the open SL-M order for this trade in order_events
        sl_order_id = self._find_open_sl_order(trade)
        if not sl_order_id:
            logger.warning(
                "No open SL-M order found for trade_id=%s — skipping modify",
                trade.id,
            )
            return

        self._modify_sl_with_retry(
            trade=trade,
            kite_order_id=sl_order_id,
            new_trigger_price=new_sl,
            new_price=new_sl,
        )

    # ── Place order with retry ────────────────────────────────

    def _place_order_with_retry(
        self,
        trade: Trade,
        transaction_type: str,
        order_type: str,
        price: Decimal,
        quantity: int,
        variety: str,
        tag: str = "",
        exit_reason: str = "",
    ) -> Optional[str]:
        """
        Place an order, retry once on failure.
        Returns kite_order_id on success, None on failure.
        """
        for attempt in range(1, 3):   # max 2 attempts
            try:
                kite = self._get_kite()
                order_params = dict(
                    variety=variety,
                    exchange=trade.instrument.exchange,
                    tradingsymbol=trade.instrument.symbol,
                    transaction_type=transaction_type,
                    quantity=quantity,
                    product=trade.product,
                    order_type=order_type,
                    price=float(price),
                    tag=tag[:20] if tag else None,
                )
                if order_type in {KiteConnect.ORDER_TYPE_MARKET, KiteConnect.ORDER_TYPE_SLM}:
                    order_id = place_protected_market_order(kite, **order_params)
                else:
                    order_id = kite.place_order(**order_params)

                logger.info(
                    "Order placed — kite_order_id=%s trade_id=%s attempt=%d",
                    order_id, trade.id, attempt,
                )
                self._state.record_success()
                self._log_order_event(
                    trade_id=trade.id,
                    kite_order_id=order_id,
                    order_type=order_type,
                    transaction_type=transaction_type,
                    variety=variety,
                    status="OPEN",
                    price=price,
                    quantity=Decimal(quantity),
                )

                # Mark trade as closed in DB if this was an exit
                if exit_reason:
                    self._close_trade_in_db(trade, price, exit_reason)

                return order_id

            except (InputException, OrderException) as exc:
                # Non-retryable rejection from Kite
                logger.error(
                    "Order rejected (attempt %d) trade_id=%s: %s",
                    attempt, trade.id, exc,
                )
                self._handle_rejection(trade, str(exc), retryable=False)
                self._log_order_event(
                    trade_id=trade.id,
                    kite_order_id="",
                    order_type=order_type,
                    transaction_type=transaction_type,
                    variety=variety,
                    status="REJECTED",
                    price=price,
                    quantity=Decimal(quantity),
                    status_message=str(exc),
                )
                return None   # no retry for hard rejections

            except (NetworkException, Exception) as exc:
                # Retryable — network glitch or transient error
                logger.warning(
                    "Order attempt %d failed trade_id=%s: %s",
                    attempt, trade.id, exc,
                )
                if attempt < 2:
                    time.sleep(RETRY_DELAY_SECONDS)
                    continue
                # Second attempt also failed
                self._handle_rejection(trade, str(exc), retryable=True)
                self._log_order_event(
                    trade_id=trade.id,
                    kite_order_id="",
                    order_type=order_type,
                    transaction_type=transaction_type,
                    variety=variety,
                    status="REJECTED",
                    price=price,
                    quantity=Decimal(quantity),
                    status_message=str(exc),
                )
                return None

        return None

    # ── Modify SL order with retry ────────────────────────────

    def _modify_sl_with_retry(
        self,
        trade: Trade,
        kite_order_id: str,
        new_trigger_price: Decimal,
        new_price: Decimal,
    ) -> bool:
        """Modify an existing SL-M order. Returns True on success."""
        for attempt in range(1, 3):
            try:
                kite = self._get_kite()
                kite.modify_order(
                    variety=KiteConnect.VARIETY_REGULAR,
                    order_id=kite_order_id,
                    order_type=KiteConnect.ORDER_TYPE_SLM,
                    trigger_price=float(new_trigger_price),
                    price=float(new_price),
                )
                logger.info(
                    "SL-M order modified — kite_order_id=%s new_sl=%.2f attempt=%d",
                    kite_order_id, new_trigger_price, attempt,
                )
                self._state.record_success()
                self._log_order_event(
                    trade_id=trade.id,
                    kite_order_id=kite_order_id,
                    order_type=KiteConnect.ORDER_TYPE_SLM,
                    transaction_type=(
                        KiteConnect.TRANSACTION_TYPE_SELL
                        if trade.direction == "BUY"
                        else KiteConnect.TRANSACTION_TYPE_BUY
                    ),
                    variety=KiteConnect.VARIETY_REGULAR,
                    status="MODIFIED",
                    price=new_price,
                    trigger_price=new_trigger_price,
                    quantity=trade.quantity,
                )
                return True

            except (InputException, OrderException) as exc:
                logger.error(
                    "SL modify rejected (attempt %d) order_id=%s: %s",
                    attempt, kite_order_id, exc,
                )
                self._handle_rejection(trade, f"SL modify: {exc}", retryable=False)
                return False

            except (NetworkException, Exception) as exc:
                logger.warning(
                    "SL modify attempt %d failed order_id=%s: %s",
                    attempt, kite_order_id, exc,
                )
                if attempt < 2:
                    time.sleep(RETRY_DELAY_SECONDS)
                    continue
                self._handle_rejection(trade, f"SL modify: {exc}", retryable=True)
                return False

        return False

    # ── Rejection handling ────────────────────────────────────

    def _handle_rejection(
        self,
        trade: Trade,
        reason: str,
        retryable: bool,
    ) -> None:
        self._state.record_rejection()

        symbol = trade.instrument.symbol if trade.instrument else str(trade.instrument_id)
        self._alerter.order_rejected(symbol, reason)

        if self._state.should_halt():
            halt_msg = (
                f"Trading halted after {MAX_CONSECUTIVE_REJECTIONS} consecutive "
                f"rejections. Last: {reason}"
            )
            self._state.halt(halt_msg)
            self._alerter.send(
                f"🚨 <b>Trading HALTED</b>\n"
                f"Reason: {reason}\n"
                f"Consecutive rejections: {self._state.consecutive_rejections}\n"
                f"Call <code>executor.resume()</code> to restart."
            )

    # ── Helpers ───────────────────────────────────────────────

    def _get_kite(self) -> KiteConnect:
        with get_db() as db:
            link = ExchangeLinkRepo.get_for_user(db, self.user_id)
            if not link or not link.is_session_valid:
                raise RuntimeError("Session expired — refresh token via Rails UI")
            api_key = link.decrypt_access_id(db)
            access_token = link.decrypt_session_token(db)

        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(access_token)
        return kite

    def _find_open_sl_order(self, trade: Trade) -> Optional[str]:
        """
        Find the most recent open SL-M order for this trade from order_events.
        """
        with get_db() as db:
            from sqlalchemy import select, and_
            from trading.models import OrderEvent as OE
            row = db.scalar(
                select(OE.kite_order_id)
                .where(
                    and_(
                        OE.trade_id   == trade.id,
                        OE.order_type == KiteConnect.ORDER_TYPE_SLM,
                        OE.status.in_(["OPEN", "MODIFIED"]),
                    )
                )
                .order_by(OE.placed_at.desc())
                .limit(1)
            )
            return row

    def _close_trade_in_db(
        self,
        trade: Trade,
        exit_price: Decimal,
        exit_reason: str,
    ) -> None:
        """Mark trade as closed and write P&L to DB."""
        with get_db() as db:
            db_trade = TradeRepo.get_by_id(db, trade.id)
            if db_trade and db_trade.is_open:
                db_trade.close(exit_price=exit_price, exit_reason=exit_reason)
                logger.info(
                    "Trade closed in DB — trade_id=%s pnl=%.2f",
                    trade.id, db_trade.pnl or 0,
                )

    def _log_order_event(
        self,
        trade_id: uuid.UUID,
        kite_order_id: str,
        order_type: str,
        transaction_type: str,
        variety: str,
        status: str,
        price: Decimal,
        quantity: Decimal,
        trigger_price: Optional[Decimal] = None,
        status_message: Optional[str]    = None,
    ) -> None:
        """Write an OrderEvent row to DB for every order attempt."""
        try:
            with get_db() as db:
                event = OrderEvent(
                    trade_id=trade_id,
                    kite_order_id=kite_order_id or "FAILED",
                    order_type=order_type,
                    transaction_type=transaction_type,
                    variety=variety,
                    status=status,
                    status_message=status_message,
                    price=price,
                    trigger_price=trigger_price,
                    quantity=quantity,
                    filled_quantity=Decimal("0"),
                    placed_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
                db.add(event)
        except Exception as exc:
            logger.error("Failed to log order event: %s", exc)

    def _load_config(self) -> None:
        with get_db() as db:
            cfg = MarketConfigRepo.get_all(db, self.user_id)
        self._alerter = Alerter(
            bot_token=cfg.get("telegram_bot_token", ""),
            chat_id=cfg.get("telegram_chat_id", ""),
        )
        self._initialised = True
        logger.info("OrderExecutor initialised")
