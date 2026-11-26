"""Broker-evidence-only reconciliation of unresolved Noise Area strategy trades."""

import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from strategies.nifty_noise_area import KEY
from trading.strategy_records import StrategyEvent, StrategyRun, StrategyTrade

log = logging.getLogger(__name__)


def confirmed_fill(kite, order_id, quantity):
    if not order_id:
        return None
    history = kite.order_history(order_id)
    if not history:
        return None
    order = history[-1]
    if (str(order.get("order_id")) not in ("None", str(order_id)) or
            order.get("status") != "COMPLETE" or
            int(order.get("filled_quantity") or 0) != quantity):
        return None
    premium = Decimal(str(order.get("average_price") or 0))
    if not premium.is_finite() or premium <= 0:
        return None
    return premium, order


def reconcile_noise_area(factory, kite, user_id, dates):
    """Never infer an exit from zero holdings or an index move."""
    repaired = 0
    with factory() as db:
        rows = db.scalars(select(StrategyTrade).where(
            StrategyTrade.user_id == user_id, StrategyTrade.strategy_key == KEY,
            StrategyTrade.mode == "live", StrategyTrade.trading_date.in_(dates),
            StrategyTrade.status.in_(("entry_pending", "open", "exit_pending", "reconcile")))).all()
        for trade in rows:
            try:
                entry = confirmed_fill(kite, trade.entry_order_id, trade.quantity)
                # The broker stop may have filled before its cancellation was observed.
                exits = [(name, oid, confirmed_fill(kite, oid, trade.quantity)) for name, oid in (
                    ("strategy_exit", trade.exit_order_id), ("broker_stop", trade.protection_order_id)) if oid]
                filled = [(name, oid, result) for name, oid, result in exits if result]
                if not entry or len(filled) != 1:
                    log.warning("Noise Area trade %s remains %s: entry or unique exit fill unverified",
                                trade.id, trade.status)
                    continue
                name, exit_id, (exit_price, exit_order) = filled[0]
                entry_price, entry_order = entry
                # Verify broker identity and direction when the history includes them.
                if any(order.get("tradingsymbol") not in (None, trade.option_symbol)
                       for order in (entry_order, exit_order)):
                    log.warning("Noise Area trade %s: broker symbol mismatch", trade.id)
                    continue
                if (entry_order.get("transaction_type") not in (None, "BUY") or
                        exit_order.get("transaction_type") not in (None, "SELL")):
                    log.warning("Noise Area trade %s: broker side mismatch", trade.id)
                    continue
                if any(order.get("exchange") not in (None, "NFO") or
                       order.get("product") not in (None, "MIS")
                       for order in (entry_order, exit_order)):
                    log.warning("Noise Area trade %s: broker exchange/product mismatch", trade.id)
                    continue
                trade.entry_premium = entry_price
                trade.exit_premium = exit_price
                trade.exit_order_id = str(exit_id)
                trade.entry_confirmed_at = trade.entry_confirmed_at or entry_order.get("exchange_timestamp")
                trade.exit_confirmed_at = trade.exit_confirmed_at or exit_order.get("exchange_timestamp")
                trade.exit_reason = name if name == "broker_stop" else (trade.exit_reason or name)
                trade.gross_option_pnl = (exit_price - entry_price) * trade.quantity
                # Index points require an observed exit index, never option P&L as a substitute.
                if trade.exit_index is not None:
                    trade.index_points = (Decimal(1) if trade.side == "CE" else Decimal(-1)) * (trade.exit_index - trade.entry_index)
                trade.status = "closed"
                trade.error_message = "Closed from confirmed broker entry and exit order history."
                trade.updated_at = datetime.now(timezone.utc)
                db.add(StrategyEvent(strategy_run_id=trade.strategy_run_id, strategy_trade_id=trade.id,
                                     occurred_at=trade.updated_at, action="BROKER_RECONCILED",
                                     reason=trade.exit_reason, details={"entry_order_id": str(trade.entry_order_id),
                                     "exit_order_id": str(exit_id), "source": "broker_order_history"}))
                repaired += 1
            except Exception as exc:
                log.warning("Noise Area trade %s: broker reconciliation unavailable (%s)",
                            trade.id, type(exc).__name__)
        for run_id in {row.strategy_run_id for row in rows if row.status == "closed"}:
            run = db.get(StrategyRun, run_id)
            if run and run.status == "failed" and not db.scalar(select(StrategyTrade.id).where(
                    StrategyTrade.strategy_run_id == run_id,
                    StrategyTrade.status != "closed").limit(1)):
                run.status = "completed"
                run.updated_at = datetime.now(timezone.utc)
    return repaired
