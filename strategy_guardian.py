"""Independent option-position guardian for recorded live strategies.

Run as a separate service. It never opens positions. On any uncertain order or
broker mismatch it fails closed and records reconciliation instead of guessing.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone, time as clock_time
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from nifty_15m_env import load_nifty_environment
load_nifty_environment()

from market.kite_orders import place_protected_market_order
from trading.database import get_db
from trading.strategy_records import StrategyEvent, StrategyTrade
from trading.strategy_risk import StrategyGuardianStatus, StrategyRiskSetting


log = logging.getLogger(__name__)
FINAL = {"COMPLETE", "REJECTED", "CANCELLED"}
QUOTE_FAILURES = {}


def heartbeat(user_id, strategy_key, status="healthy", message=None):
    now = datetime.now(timezone.utc)
    with get_db() as db:
        row = db.scalar(select(StrategyGuardianStatus).where(
            StrategyGuardianStatus.user_id == user_id,
            StrategyGuardianStatus.strategy_key == strategy_key,
        ).with_for_update())
        if row is None:
            row = StrategyGuardianStatus(user_id=user_id, strategy_key=strategy_key,
                                         created_at=now)
            db.add(row)
        row.heartbeat_at = now
        row.status = status
        row.message = message
        row.updated_at = now


def reserve_exit(trade_id, reason, *, recover_pending=False):
    with get_db() as db:
        row = db.get(StrategyTrade, trade_id, with_for_update=True)
        if row.status != "open" and not (recover_pending and row.status == "exit_pending"):
            return False
        row.status = "exit_pending"
        row.exit_reason = reason
        row.exit_signal_time = datetime.now(timezone.utc)
        row.updated_at = row.exit_signal_time
        return True


def mark_reconcile(trade_id, message):
    with get_db() as db:
        row = db.get(StrategyTrade, trade_id, with_for_update=True)
        if row.status != "closed":
            row.status = "reconcile"
            row.error_message = message[:2000]
            row.updated_at = datetime.now(timezone.utc)


def finish_exit(trade_id, order_id, premium, reason, quote=None):
    now = datetime.now(timezone.utc)
    with get_db() as db:
        row = db.get(StrategyTrade, trade_id, with_for_update=True)
        if row.status == "closed":
            return
        if row.status not in {"open", "exit_pending"}:
            raise RuntimeError(f"Trade {trade_id} is {row.status}; manual reconciliation required")
        row.exit_order_id = str(order_id)
        row.exit_premium = Decimal(str(premium))
        row.exit_confirmed_at = now
        row.exit_signal_time = row.exit_signal_time or now
        row.exit_reason = reason
        row.gross_option_pnl = (row.exit_premium - row.entry_premium) * row.quantity
        row.status = "closed"
        row.updated_at = now
        db.add(StrategyEvent(
            strategy_run_id=row.strategy_run_id, strategy_trade_id=row.id,
            occurred_at=now, action=f"EXIT_{row.side}", reason=reason,
            details={"option_symbol": row.option_symbol,
                     "quote": str(quote) if quote is not None else None,
                     "exit_order_id": str(order_id)},
        ))


def order_last(kite, order_id):
    history = kite.order_history(order_id)
    if not history:
        raise RuntimeError(f"No broker history for order {order_id}")
    return history[-1]


def cancel_protection(kite, trade):
    if not trade.protection_order_id:
        return None
    order = order_last(kite, trade.protection_order_id)
    if order["status"] == "COMPLETE":
        if int(order.get("filled_quantity") or 0) != trade.quantity:
            raise RuntimeError("Broker stop filled an unexpected quantity")
        return order
    if order["status"] in {"REJECTED", "CANCELLED"}:
        return None
    kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=trade.protection_order_id)
    for _ in range(10):
        order = order_last(kite, trade.protection_order_id)
        if order["status"] in FINAL:
            if order["status"] == "COMPLETE" and int(order.get("filled_quantity") or 0) != trade.quantity:
                raise RuntimeError("Broker stop filled an unexpected quantity")
            return order if order["status"] == "COMPLETE" else None
        time.sleep(0.5)
    raise RuntimeError("Protection cancellation not confirmed; refusing duplicate sell")


def emergency_exit(kite, trade, reason, quote=None, *, recover_pending=False):
    if getattr(trade, 'strategy_key', None) == 'nifty_30m_reference':
        from trading.trade_execution_lock import trade_execution_lock
        with trade_execution_lock(trade.id) as acquired:
            if not acquired:
                return
            return _emergency_exit_owned(kite,trade,reason,quote,recover_pending=recover_pending)
    return _emergency_exit_owned(kite,trade,reason,quote,recover_pending=recover_pending)


def _emergency_exit_owned(kite, trade, reason, quote=None, *, recover_pending=False):
    if not reserve_exit(trade.id, reason, recover_pending=recover_pending):
        return
    try:
        completed = cancel_protection(kite, trade)
        if completed is not None:
            finish_exit(trade.id, trade.protection_order_id,
                        completed["average_price"], "broker_stop", quote)
            return
        positions = kite.positions().get("net", [])
        quantity = sum(int(p.get("quantity") or 0) for p in positions
                       if p.get("exchange") == "NFO"
                       and p.get("tradingsymbol") == trade.option_symbol
                       and p.get("product") == kite.PRODUCT_MIS)
        if quantity != trade.quantity:
            raise RuntimeError(f"Broker quantity {quantity} differs from strategy lot {trade.quantity}")
        if any(o.get("exchange") == "NFO" and o.get("tradingsymbol") == trade.option_symbol
               and o.get("status") not in FINAL for o in kite.orders()):
            raise RuntimeError("Another order is pending on the strategy contract")
        order_id = place_protected_market_order(
            kite, variety=kite.VARIETY_REGULAR, exchange="NFO",
            tradingsymbol=trade.option_symbol, transaction_type=kite.TRANSACTION_TYPE_SELL,
            quantity=trade.quantity, product=kite.PRODUCT_MIS,
            order_type=kite.ORDER_TYPE_MARKET, validity=kite.VALIDITY_DAY,
            tag="STRATEGYGUARD",
        )
        with get_db() as db:
            db.get(StrategyTrade, trade.id).exit_order_id = str(order_id)
        for _ in range(20):
            order = order_last(kite, order_id)
            if order["status"] == "COMPLETE":
                if int(order.get("filled_quantity") or 0) != trade.quantity:
                    raise RuntimeError("Emergency exit filled an unexpected quantity")
                finish_exit(trade.id, order_id, order["average_price"], reason, quote)
                return
            if order["status"] in {"REJECTED", "CANCELLED"}:
                raise RuntimeError(f"Emergency exit {order_id} {order['status']}")
            time.sleep(1)
        raise RuntimeError(f"Emergency exit {order_id} not confirmed")
    except Exception as exc:
        mark_reconcile(trade.id, str(exc))
        log.exception("Trade %s requires immediate broker reconciliation", trade.id)
        raise


def recover_pending_trade(kite, trade):
    """Recover only broker-confirmed fills; never infer a missing order ID."""
    now = datetime.now(timezone.utc)
    if trade.status == "entry_pending":
        if not trade.entry_order_id:
            if now - trade.updated_at > timedelta(seconds=30):
                mark_reconcile(trade.id, "Entry order ID missing; inspect broker positions")
            return
        order = order_last(kite, trade.entry_order_id)
        if order["status"] == "COMPLETE" and int(order.get("filled_quantity") or 0) == trade.quantity:
            with get_db() as db:
                row = db.get(StrategyTrade, trade.id, with_for_update=True)
                if row.status == "entry_pending":
                    row.entry_premium = Decimal(str(order["average_price"]))
                    row.entry_confirmed_at = now
                    row.status = "open"
                    row.updated_at = now
            # No broker stop can be assumed after a crash in the entry path.
            with get_db() as db:
                recovered = db.get(StrategyTrade, trade.id)
                db.expunge(recovered)
            emergency_exit(kite, recovered, "entry_recovered_without_protection")
        elif order["status"] in {"REJECTED", "CANCELLED"}:
            mark_reconcile(trade.id, f"Entry order {order['status']}; verify no partial fill")
        return

    if trade.status != "exit_pending":
        return
    if trade.exit_order_id:
        order = order_last(kite, trade.exit_order_id)
        if order["status"] == "COMPLETE" and int(order.get("filled_quantity") or 0) == trade.quantity:
            finish_exit(trade.id, trade.exit_order_id, order["average_price"],
                        trade.exit_reason or "recovered_exit")
            return
        if order["status"] not in {"REJECTED", "CANCELLED"}:
            if now - trade.updated_at > timedelta(seconds=30):
                heartbeat(trade.user_id, trade.strategy_key, "degraded",
                          f"Exit order {trade.exit_order_id} still pending; verify broker")
            return  # A pending broker sell must never be duplicated.
    if now - trade.updated_at < timedelta(seconds=60):
        return
    emergency_exit(kite, trade, "stale_exit_pending", recover_pending=True)


def monitor_cycle(kite, user_id):
    with get_db() as db:
        # Disabling entries must never remove protection from existing trades.
        settings = list(db.scalars(select(StrategyRiskSetting).where(
            StrategyRiskSetting.user_id == user_id,
        )))
        trades = list(db.scalars(select(StrategyTrade).where(
            StrategyTrade.user_id == user_id,
            StrategyTrade.mode == "live",
            StrategyTrade.status.in_(["open", "entry_pending", "exit_pending", "reconcile"]),
        )))
        setting_by_key = {s.strategy_key: s for s in settings}
        monitored = [t for t in trades if t.strategy_key in setting_by_key]
        snapshots = [(t.id, t.strategy_key, t.option_symbol)
                     for t in monitored if t.status == "open"]
        db.expunge_all()
    try:
        quotes = kite.quote([f"NFO:{symbol}" for _, _, symbol in snapshots]) if snapshots else {}
    except Exception:
        log.exception("Option quotes unavailable; evaluating emergency policy")
        quotes = {}
    for key in setting_by_key:
        heartbeat(user_id, key)
    for trade in monitored:
        setting = setting_by_key[trade.strategy_key]
        try:
            if trade.status == "reconcile":
                heartbeat(user_id, trade.strategy_key, "degraded",
                          f"Trade {trade.id} requires manual broker reconciliation")
                continue
            if trade.status != "open":
                recover_pending_trade(kite, trade)
                continue
            if not trade.entry_premium:
                raise RuntimeError("Open trade has no confirmed entry premium")
            if trade.protection_order_id:
                order = order_last(kite, trade.protection_order_id)
                if order["status"] == "COMPLETE":
                    if int(order.get("filled_quantity") or 0) != trade.quantity:
                        raise RuntimeError("Broker stop filled an unexpected quantity")
                    finish_exit(trade.id, trade.protection_order_id,
                                order["average_price"], "broker_stop")
                    continue
                if order["status"] in {"REJECTED", "CANCELLED"}:
                    raise RuntimeError(f"Broker stop order {order['status']}")
            elif setting.stop_mode == "broker_stop_limit":
                # Allow the primary runner a short interval to record protection.
                if trade.entry_confirmed_at and datetime.now(timezone.utc) - trade.entry_confirmed_at < timedelta(seconds=10):
                    continue
                raise RuntimeError("Broker stop order missing")

            quote = quotes.get(f"NFO:{trade.option_symbol}")
            if not quote or not quote.get("depth", {}).get("buy"):
                raise RuntimeError("No current option bid quote")
            bid = Decimal(str(quote["depth"]["buy"][0]["price"]))
            quoted_at = quote.get("timestamp")
            if quoted_at is None or bid <= 0:
                raise RuntimeError("Option bid is unavailable")
            if quoted_at.tzinfo is None:
                quoted_at = quoted_at.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
            if datetime.now(timezone.utc) - quoted_at > timedelta(seconds=20):
                raise RuntimeError("Option quote is stale")
            pnl = (bid - trade.entry_premium) * trade.quantity
            with get_db() as db:
                daily = Decimal(db.scalar(select(func.coalesce(func.sum(StrategyTrade.gross_option_pnl), 0)).where(
                    StrategyTrade.user_id == user_id,
                    StrategyTrade.strategy_key == trade.strategy_key,
                    StrategyTrade.mode == "live",
                    StrategyTrade.trading_date == trade.trading_date,
                    StrategyTrade.status == "closed",
                )) or 0)
            if pnl <= -setting.risk_per_trade:
                emergency_exit(kite, trade, "hard_risk_per_trade", bid)
            elif daily + pnl <= -setting.max_daily_loss:
                emergency_exit(kite, trade, "hard_daily_loss", bid)
            elif trade.protection_trigger and bid <= trade.protection_trigger:
                emergency_exit(kite, trade, "stop_limit_not_filled", bid)
            QUOTE_FAILURES.pop(trade.id, None)
        except Exception as exc:
            heartbeat(user_id, trade.strategy_key, "degraded", str(exc)[:500])
            if "quote" in str(exc).lower() and trade.protection_order_id:
                QUOTE_FAILURES[trade.id] = QUOTE_FAILURES.get(trade.id, 0) + 1
                if QUOTE_FAILURES[trade.id] < 3:
                    log.warning("Quote unavailable for protected trade %s (%s/3); retrying",
                                trade.id, QUOTE_FAILURES[trade.id])
                    continue
            if setting.adverse_action == "emergency_exit" and trade.status == "open":
                emergency_exit(kite, trade, "protection_unavailable")
            else:
                log.exception("Unprotected trade %s; user action required", trade.id)


def main():
    from nifty_15m_env import load_nifty_environment
    load_nifty_environment()
    from trading.user_token import fetch_user_token
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ist = ZoneInfo("Asia/Kolkata")
    started = datetime.now(ist).date()
    while datetime.now(ist).date() == started and datetime.now(ist).time() < clock_time(15, 45):
        try:
            kite, user_id = fetch_user_token(log)
            kite.profile()
            monitor_cycle(kite, user_id)
        except SystemExit as exc:
            if exc.code != 1:
                raise
            log.warning("Kite guardian token unavailable; retrying in 60 seconds")
            time.sleep(60)
        except Exception:
            log.exception("Strategy guardian failed; check open broker positions immediately")
            time.sleep(15)
        time.sleep(5)


if __name__ == "__main__":
    main()
