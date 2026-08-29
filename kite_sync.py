"""
kite_sync.py — sync Kite order history, trades and positions into DB.

Pulls from Kite:
  - Trade book (executed orders) for a date range
  - Open positions (current holdings)
  - P&L for closed trades

Also creates investment_transactions rows for every COMPLETE order synced,
using order_id as the dedup key so reruns are safe.

Usage:
    python kite_sync.py                          # last 30 days
    python kite_sync.py --from 2026-01-01 --to 2026-03-22
    python kite_sync.py --only positions
    python kite_sync.py --only trades
    python kite_sync.py --dry-run

Options:
    --from      Start date (YYYY-MM-DD). Default: 30 days ago
    --to        End date   (YYYY-MM-DD). Default: today
    --only      trades | positions | all (default: all)
    --dry-run   Print summary without writing to DB
"""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

from dotenv import load_dotenv
load_dotenv()
from database.db_record_addition.db_record_create_or_fetch import get_or_create_instrument
from utilities.conversion.number_conversion import to_dec



from trading.user_token import fetch_user_token
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from trading.database import get_db
from trading.models import Instrument, OrderEvent, Position, Trade

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("kite_sync")


# ─────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────

# Maps Kite product codes → investment_transactions.order_type
PRODUCT_TO_ORDER_TYPE = {
    "MIS":   "Intraday",
    "CNC":   "Delivery",
    "NRML":  "Normal",
    "CO":    "Intraday",   # cover order — intraday
    "BO":    "Intraday",   # bracket order — intraday
}

# Maps Kite transaction_type → investment_transactions.txn_type
TXN_TYPE_MAP = {
    "BUY":  "Buy",
    "SELL": "Sell",
}


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def parse_kite_ts(ts) -> Optional[datetime]:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts
    try:
        return datetime.strptime(str(ts), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def get_investment_id(db: Session, user_id: uuid.UUID) -> Optional[int]:
    """
    Fetch the integer investment_id for this user from the investments table.
    Cached after the first call per run.
    """
    result = db.execute(
        text("SELECT id FROM investments LIMIT 1"),
        {},
    ).fetchone()

    if result is None:
        logger.error("No investments row found for user_id=%s", user_id)
        return None

    return result[0]


def investment_txn_exists(db: Session, order_id: str) -> bool:
    """Check if an investment_transaction already exists for this Kite order_id."""
    result = db.execute(
        text("SELECT 1 FROM investment_transactions WHERE txn_id = :txn_id LIMIT 1"),
        {"txn_id": order_id},
    ).fetchone()
    return result is not None


# ─────────────────────────────────────────────────────────────
# Investment transaction creation
# ─────────────────────────────────────────────────────────────

def create_investment_transaction(
    db: Session,
    order: dict,
    instrument_id: int,
    investment_id: int,
) -> bool:
    """
    Insert one investment_transaction row from a COMPLETE Kite order.

    Mapping:
      txn_id          ← order_id          (dedup key — safe to rerun)
      reference_number← exchange_order_id
      txn_type        ← BUY→Buy / SELL→Sell
      quantity        ← filled_quantity
      amount          ← average_price     (price per unit)
      order_type      ← MIS→Intraday / CNC→Delivery / NRML→Normal
      txn_date        ← date portion of order_timestamp
      txn_time        ← HH:MM:SS portion of order_timestamp
      brokerages      ← 0  (not available in Kite order response)
      charges         ← 0  (not available in Kite order response)
      currency        ← INR (hardcoded — Kite India only)
      pledge          ← false

    Returns True if inserted, False if skipped (already exists or error).
    """
    order_id = order.get("order_id", "")

    # Dedup — safe to rerun without creating duplicates
    if investment_txn_exists(db, order_id):
        logger.debug("  investment_transaction already exists for order_id=%s — skipping", order_id)
        return False

    ts         = parse_kite_ts(order.get("order_timestamp"))
    txn_date   = ts.date()                          if ts else date.today()
    txn_time   = ts.strftime("%H:%M:%S")            if ts else "00:00:00"
    txn_type   = TXN_TYPE_MAP.get(order.get("transaction_type", ""), "Buy")
    order_type = PRODUCT_TO_ORDER_TYPE.get(order.get("product", "MIS"), "Intraday")
    quantity   = to_dec(order.get("filled_quantity", 0))
    amount     = to_dec(order.get("average_price", 0))   # price per unit
    exchange   = order.get("exchange", "")
    ref_number = order.get("exchange_order_id") or ""

    db.execute(
        text("""
            INSERT INTO investment_transactions (
                investment_id,
                instrument_type,
                instrument_id,
                txn_type,
                currency,
                quantity,
                amount,
                brokrages,
                charges,
                txn_date,
                txn_id,
                reference_number,
                exchange,
                txn_time,
                pledge,
                created_at,
                updated_at
            ) VALUES (
                :investment_id,
                'Instrument',
                :instrument_id,
                :txn_type,
                'INR',
                :quantity,
                :amount,
                0,
                0,
                :txn_date,
                :txn_id,
                :reference_number,
                :exchange,
                :txn_time,
                false,
                NOW(),
                NOW()
            )
        """),
        {
            "investment_id":  investment_id,
            "instrument_id":  instrument_id,
            "txn_type":       txn_type,
            "quantity":       quantity,
            "amount":         amount,
            "txn_date":       txn_date,
            "txn_id":         order_id,
            "reference_number": ref_number,
            "exchange":       exchange,
            "txn_time":       txn_time,
        },
    )

    logger.info(
        "  Created investment_transaction: %s %s qty=%s @ ₹%s",
        txn_type, order.get("tradingsymbol"), quantity, amount,
    )
    return True


# ─────────────────────────────────────────────────────────────
# Trade sync
# ─────────────────────────────────────────────────────────────

def sync_trades(
    kite,
    db: Session,
    user_id: uuid.UUID,
    from_date: date,
    to_date: date,
    dry_run: bool = False,
) -> dict:
    """
    Fetch Kite orders for date range.
    Maps each COMPLETE order to:
      - Trade + OrderEvent rows (grouped by symbol+direction+date)
      - One investment_transaction row per order (deduplicated by order_id)
    """
    logger.info("Fetching Kite trades from %s to %s", from_date, to_date)

    stats = {
        "new_trades":           0,
        "updated_orders":       0,
        "skipped":              0,
        "errors":               0,
        "investment_txns_added": 0,
    }

    # Resolve investment_id once for the entire run
    investment_id = get_investment_id(db, user_id)
    if investment_id is None:
        logger.error("Cannot sync — no investment_id found for user")
        stats["errors"] += 1
        return stats

    logger.info("Resolved investment_id=%d for user_id=%s", investment_id, user_id)

    try:
        all_orders = kite.orders()
    except Exception as exc:
        logger.error("Failed to fetch orders from Kite: %s", exc)
        stats["errors"] += 1
        return stats

    logger.info("Fetched %d orders from Kite", len(all_orders))

    # Filter to date range
    filtered = []
    for order in all_orders:
        ts = parse_kite_ts(order.get("order_timestamp"))
        if ts is None:
            continue
        if from_date <= ts.date() <= to_date:
            filtered.append(order)

    logger.info("%d orders in date range %s – %s", len(filtered), from_date, to_date)

    # Group COMPLETE orders by symbol+exchange+direction+date → one Trade per group
    from collections import defaultdict
    groups: dict = defaultdict(list)
    for order in filtered:
        if order.get("status") != "COMPLETE":
            continue
        ts  = parse_kite_ts(order.get("order_timestamp"))
        key = (
            order["tradingsymbol"],
            order["exchange"],
            order["transaction_type"],
            ts.date() if ts else from_date,
        )
        groups[key].append(order)

    for (symbol, exchange, direction, trade_date), orders in groups.items():
        try:
            _upsert_trade_group(
                db, kite, user_id, investment_id,
                symbol, exchange, direction, trade_date, orders,
                dry_run, stats,
            )
        except Exception as exc:
            logger.error(
                "Error processing %s %s %s: %s", symbol, exchange, direction, exc,
                exc_info=True,
            )
            stats["errors"] += 1

    return stats


def _upsert_trade_group(
    db, kite, user_id, investment_id,
    symbol, exchange, direction, trade_date, orders,
    dry_run, stats,
):
    # Check if a trade already exists for this symbol+direction+date
    existing = db.scalar(
        select(Trade).where(
            Trade.user_id    == user_id,
            Trade.direction  == direction,
            Trade.entered_at >= datetime.combine(trade_date, datetime.min.time()).replace(tzinfo=timezone.utc),
            Trade.entered_at <  datetime.combine(trade_date + timedelta(days=1), datetime.min.time()).replace(tzinfo=timezone.utc),
        ).join(Trade.instrument).where(
            Instrument.symbol   == symbol,
            Instrument.exchange == exchange,
        )
    )

    # Compute aggregate values from all orders in the group
    total_qty   = sum(to_dec(o.get("filled_quantity", 0)) for o in orders)
    total_value = sum(
        to_dec(o.get("average_price", 0)) * to_dec(o.get("filled_quantity", 0))
        for o in orders
    )
    avg_price = (total_value / total_qty).quantize(Decimal("0.05")) if total_qty > 0 else Decimal("0")
    first_ts  = min(
        (parse_kite_ts(o.get("order_timestamp")) for o in orders if parse_kite_ts(o.get("order_timestamp"))),
        default=datetime.now(timezone.utc),
    )

    if dry_run:
        logger.info(
            "[DRY RUN] %s %s %s | qty=%s avg_price=%s | %d order(s) → would create %d investment_txn(s)",
            "UPDATE" if existing else "NEW",
            direction, symbol, total_qty, avg_price, len(orders), len(orders),
        )
        stats["new_trades" if not existing else "skipped"] += 1
        return

    inst = get_or_create_instrument(db, kite, symbol, exchange, logger)
    if not inst:
        stats["errors"] += 1
        return

    # ── Trade upsert ──────────────────────────────────────────────────────
    if not existing:
        product    = orders[0].get("product", "MIS")
        trade_type = "EQUITY"
        if exchange in ("NFO", "BFO"):
            trade_type = "OPTIONS" if symbol.endswith(("CE", "PE")) else "FUTURES"
        elif exchange == "MCX":
            trade_type = "FUTURES"

        trade = Trade(
            instrument_id = inst.id,
            user_id       = user_id,
            direction     = direction,
            trade_type    = trade_type,
            product       = product,
            entry_price   = avg_price,
            quantity      = total_qty,
            entry_value   = total_value,
            initial_sl    = avg_price,
            current_sl    = avg_price,
            risk_amount   = Decimal("0"),
            sl_method     = "manual",
            status        = "open",
            entered_at    = first_ts,
        )
        db.add(trade)
        db.flush()
        stats["new_trades"] += 1
        logger.info("Created trade: %s %s @ ₹%s qty=%s", direction, symbol, avg_price, total_qty)
    else:
        trade = existing

    # ── OrderEvent upsert ─────────────────────────────────────────────────
    for order in orders:
        kite_id        = order.get("order_id", "")
        existing_event = db.scalar(
            select(OrderEvent).where(OrderEvent.order_id == kite_id)
        )
        new_status = order.get("status", "")

        if existing_event:
            if existing_event.status != new_status:
                existing_event.status          = new_status
                existing_event.status_message  = order.get("status_message")
                existing_event.filled_quantity = to_dec(order.get("filled_quantity", 0))
                existing_event.average_price   = to_dec(order.get("average_price", 0)) or None
                existing_event.updated_at      = parse_kite_ts(order.get("exchange_update_timestamp")) or datetime.now(timezone.utc)
                stats["updated_orders"] += 1
        else:
            event = OrderEvent(
                trade_id          = trade.id,
                order_id          = kite_id,
                parent_order_id   = order.get("parent_order_id"),
                order_type        = order.get("order_type", "MARKET"),
                transaction_type  = order.get("transaction_type", direction),
                variety           = order.get("variety", "regular"),
                status            = new_status,
                status_message    = order.get("status_message"),
                price             = to_dec(order.get("price")) or None,
                trigger_price     = to_dec(order.get("trigger_price")) or None,
                quantity          = to_dec(order.get("quantity", 0)),
                filled_quantity   = to_dec(order.get("filled_quantity", 0)),
                average_price     = to_dec(order.get("average_price")) or None,
                exchange_order_id = order.get("exchange_order_id"),
                placed_at         = parse_kite_ts(order.get("order_timestamp")) or datetime.now(timezone.utc),
                updated_at        = parse_kite_ts(order.get("exchange_update_timestamp")) or datetime.now(timezone.utc),
                created_at        = parse_kite_ts(order.get("exchange_order_timestamp")) or datetime.now(timezone.utc),
            )
            db.add(event)
            stats["updated_orders"] += 1

        # ── investment_transaction — one per order, deduplicated by order_id ──
        inserted = create_investment_transaction(
            db, order, inst.id, investment_id
        )
        if inserted:
            stats["investment_txns_added"] += 1


# ─────────────────────────────────────────────────────────────
# Position sync
# ─────────────────────────────────────────────────────────────

def sync_positions(
    kite,
    db: Session,
    user_id: uuid.UUID,
    dry_run: bool = False,
) -> dict:
    """
    Fetch current open positions from Kite and upsert into positions table.
    Also syncs P&L for day positions.
    """
    logger.info("Fetching open positions from Kite...")
    stats = {"new_positions": 0, "updated_positions": 0, "closed_positions": 0, "errors": 0}

    try:
        pos_data = kite.positions()
    except Exception as exc:
        logger.error("Failed to fetch positions: %s", exc)
        stats["errors"] += 1
        return stats

    net_positions = pos_data.get("net", [])
    day_positions = pos_data.get("day", [])

    logger.info("Net positions: %d | Day positions: %d", len(net_positions), len(day_positions))

    # Build day P&L map: symbol_exchange → realised pnl
    day_pnl_map = {
        f"{p['tradingsymbol']}_{p['exchange']}": to_dec(p.get("realised", 0))
        for p in day_positions
    }

    for pos in net_positions:
        symbol   = pos["tradingsymbol"]
        exchange = pos["exchange"]
        qty      = to_dec(pos.get("quantity", 0))

        if dry_run:
            logger.info(
                "[DRY RUN] Position: %s [%s] qty=%s avg=₹%s unrealised=₹%s",
                symbol, exchange, qty,
                to_dec(pos.get("average_price", 0)),
                to_dec(pos.get("unrealised", 0)),
            )
            stats["new_positions"] += 1
            continue

        try:
            _upsert_position(db, kite, user_id, pos, day_pnl_map, stats)
        except Exception as exc:
            logger.error("Error syncing position %s: %s", symbol, exc, exc_info=True)
            stats["errors"] += 1

    return stats


def _upsert_position(db, kite, user_id, pos, day_pnl_map, stats):
    symbol   = pos["tradingsymbol"]
    exchange = pos["exchange"]
    qty      = to_dec(pos.get("quantity", 0))

    inst = get_or_create_instrument(db, kite, symbol, exchange, logger)
    if not inst:
        stats["errors"] += 1
        return

    # Find most recent open trade for this instrument
    trade = db.scalar(
        select(Trade).where(
            Trade.instrument_id == inst.id,
            Trade.user_id       == user_id,
            Trade.status        == "open",
        ).order_by(Trade.entered_at.desc()).limit(1)
    )

    # If no trade found, create a placeholder
    if not trade:
        direction = "BUY" if qty > 0 else "SELL"
        avg_price = to_dec(pos.get("average_price", 0))
        trade = Trade(
            instrument_id = inst.id,
            user_id       = user_id,
            direction     = direction,
            trade_type    = "EQUITY",
            product       = pos.get("product", "MIS"),
            entry_price   = avg_price,
            quantity      = abs(qty),
            entry_value   = avg_price * abs(qty),
            initial_sl    = avg_price,
            current_sl    = avg_price,
            risk_amount   = Decimal("0"),
            sl_method     = "manual",
            status        = "open",
            entered_at    = datetime.now(timezone.utc),
        )
        db.add(trade)
        db.flush()
        logger.info("Created placeholder trade for position: %s %s", direction, symbol)

    # Upsert position
    existing_pos = db.scalar(
        select(Position).where(Position.trade_id == trade.id)
    )

    day_key    = f"{symbol}_{exchange}"
    realised   = day_pnl_map.get(day_key, Decimal("0"))
    unrealised = to_dec(pos.get("unrealised", 0))
    avg_price  = to_dec(pos.get("average_price", 0))
    last_price = to_dec(pos.get("last_price", 0))

    if existing_pos:
        existing_pos.quantity          = abs(qty)
        existing_pos.avg_price         = avg_price
        existing_pos.last_price        = last_price
        existing_pos.unrealised_pnl    = unrealised
        existing_pos.realised_pnl      = realised
        existing_pos.day_buy_quantity  = to_dec(pos.get("day_buy_quantity", 0))
        existing_pos.day_sell_quantity = to_dec(pos.get("day_sell_quantity", 0))
        existing_pos.status            = "open" if qty != 0 else "closed"
        if qty == 0:
            existing_pos.closed_at = datetime.now(timezone.utc)
        stats["updated_positions"] += 1
        logger.info("Updated position: %s qty=%s unrealised=₹%s", symbol, qty, unrealised)
    else:
        new_pos = Position(
            instrument_id     = inst.id,
            trade_id          = trade.id,
            user_id           = user_id,
            avg_price         = avg_price,
            quantity          = abs(qty),
            last_price        = last_price,
            unrealised_pnl    = unrealised,
            realised_pnl      = realised,
            day_buy_quantity  = to_dec(pos.get("day_buy_quantity", 0)),
            day_sell_quantity = to_dec(pos.get("day_sell_quantity", 0)),
            product           = pos.get("product", "MIS"),
            status            = "open" if qty != 0 else "closed",
            opened_at         = datetime.now(timezone.utc),
        )
        db.add(new_pos)
        stats["new_positions"] += 1
        logger.info(
            "Created position: %s qty=%s avg=₹%s unrealised=₹%s",
            symbol, qty, avg_price, unrealised,
        )

    # Close trade if position qty=0
    if qty == 0 and trade.status == "open":
        total_pnl        = unrealised + realised
        trade.pnl        = total_pnl
        trade.status     = "closed"
        trade.exited_at  = datetime.now(timezone.utc)
        trade.exit_reason= "synced_from_kite"
        trade.exit_price = last_price
        stats["closed_positions"] += 1
        logger.info("Closed trade from position sync: %s pnl=₹%s", symbol, total_pnl)


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Sync Kite trade history to DB")
    parser.add_argument("--from",    dest="from_date", default=None,  help="Start date YYYY-MM-DD (default: 30 days ago)")
    parser.add_argument("--to",      dest="to_date",   default=None,  help="End date YYYY-MM-DD (default: today)")
    parser.add_argument("--only",    dest="only",      default="all", choices=["trades", "positions", "all"])
    parser.add_argument("--dry-run", dest="dry_run",   action="store_true")
    args = parser.parse_args()

    today     = date.today()
    from_date = date.fromisoformat(args.from_date) if args.from_date else today - timedelta(days=30)
    to_date   = date.fromisoformat(args.to_date)   if args.to_date   else today

    logger.info("=" * 60)
    logger.info("Kite sync  |  %s → %s  |  mode=%s  |  dry_run=%s",
                from_date, to_date, args.only, args.dry_run)
    logger.info("=" * 60)

    kite, user_id = fetch_user_token(logger)

    all_stats = {}

    with get_db() as db:
        if args.only in ("trades", "all"):
            logger.info("\n── Syncing trades ──────────────────────────────")
            all_stats["trades"] = sync_trades(
                kite, db, user_id, from_date, to_date, args.dry_run
            )

        if args.only in ("positions", "all"):
            logger.info("\n── Syncing positions ───────────────────────────")
            all_stats["positions"] = sync_positions(
                kite, db, user_id, args.dry_run
            )

    # ── Summary ───────────────────────────────────────────────
    logger.info("\n" + "=" * 60)
    logger.info("SYNC COMPLETE%s", " (DRY RUN — nothing written)" if args.dry_run else "")

    if "trades" in all_stats:
        s = all_stats["trades"]
        logger.info(
            "Trades:      new=%-4d  updated_orders=%-4d  skipped=%-4d  errors=%-4d  investment_txns=%-4d",
            s["new_trades"], s["updated_orders"], s["skipped"], s["errors"], s["investment_txns_added"],
        )
    if "positions" in all_stats:
        s = all_stats["positions"]
        logger.info(
            "Positions:   new=%-4d  updated=%-4d  closed=%-4d  errors=%-4d",
            s["new_positions"], s["updated_positions"], s["closed_positions"], s["errors"],
        )
    logger.info("=" * 60)


if __name__ == "__main__":
    main()