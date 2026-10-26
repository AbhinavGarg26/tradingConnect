"""Deterministic start-of-day trade reconciliation using Kite executions."""

from __future__ import annotations

from collections import Counter, defaultdict, deque
from datetime import datetime, time
import logging
import re
import time as monotonic_time
from typing import Iterable
from zoneinfo import ZoneInfo

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session


logger = logging.getLogger("MarketAnalytics")
IST = ZoneInfo("Asia/Kolkata")
SESSION_RESET_TIME = time(6, 0)
_TOTAL_CHARGES_CACHE: dict[tuple, float] = {}


def is_before_session_reset(now: datetime | None = None) -> bool:
    """Keep the previous trading session intact until the 06:00 IST reset."""
    moment = now or datetime.now(IST)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=IST)
    else:
        moment = moment.astimezone(IST)
    return moment.time() < SESSION_RESET_TIME


def resolve_trade_attribution(
    db: Session,
    user_id: int,
    provider: str = "zerodha",
) -> tuple[int, str] | None:
    """Resolve exactly one active broker link for safe account-scoped writes."""
    rows = db.execute(text("""
        SELECT id, user_id
        FROM exchange_links
        WHERE user_id = :user_id
          AND provider = :provider
          AND is_active = TRUE
        ORDER BY created_at DESC, id
    """), {"user_id": user_id, "provider": provider}).fetchall()
    if len(rows) != 1:
        logger.critical(
            "Trade reconciliation paused: expected one active %s link for user_id=%s, found %s",
            provider, user_id, len(rows),
        )
        return None
    return int(rows[0].user_id), str(rows[0].id)


def _execution_time(execution: dict) -> datetime:
    value = (
        execution.get("fill_timestamp")
        or execution.get("exchange_timestamp")
        or execution.get("order_timestamp")
    )
    if isinstance(value, datetime):
        if value.tzinfo:
            return value.astimezone(IST).replace(tzinfo=None)
        return value
    if not value:
        return datetime.now(IST).replace(tzinfo=None)
    raw = str(value)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        parsed = datetime.combine(datetime.now(IST).date(), time.fromisoformat(raw))
    if parsed.tzinfo:
        parsed = parsed.astimezone(IST).replace(tzinfo=None)
    return parsed


def aggregate_order_executions(executions: Iterable[dict]) -> list[dict]:
    """Combine arbitrary exchange fill chunks into stable order-level executions."""
    grouped: dict[tuple, dict] = {}
    for execution in executions:
        quantity = int(execution.get("quantity") or execution.get("filled") or 0)
        price = float(execution.get("average_price") or 0)
        if quantity <= 0 or price <= 0:
            continue
        key = (
            str(execution.get("exchange", "")),
            str(execution.get("tradingsymbol", "")),
            str(execution.get("product", "")),
            str(execution.get("transaction_type", "")),
            str(execution.get("order_id", "")),
        )
        timestamp = _execution_time(execution)
        aggregate = grouped.setdefault(key, {
            "exchange": key[0],
            "tradingsymbol": key[1],
            "product": key[2],
            "transaction_type": key[3],
            "order_id": key[4],
            "quantity": 0,
            "notional": 0.0,
            "timestamp": timestamp,
        })
        aggregate["quantity"] += quantity
        aggregate["notional"] += price * quantity
        aggregate["timestamp"] = min(aggregate["timestamp"], timestamp)

    result = []
    for aggregate in grouped.values():
        aggregate["price"] = aggregate.pop("notional") / aggregate["quantity"]
        result.append(aggregate)
    return sorted(result, key=lambda item: (item["timestamp"], item["order_id"]))


def build_fifo_trade_rows(order_executions: Iterable[dict]) -> list[dict]:
    """Replay executions and return deterministic OPEN/CLOSED trade rows."""
    books: dict[tuple, dict[str, deque]] = defaultdict(
        lambda: {"BUY": deque(), "SELL": deque()}
    )
    closed_rows: list[dict] = []

    for execution in order_executions:
        side = execution["transaction_type"]
        if side not in {"BUY", "SELL"}:
            continue
        opposite = "SELL" if side == "BUY" else "BUY"
        instrument_key = (
            execution["exchange"], execution["tradingsymbol"], execution["product"]
        )
        book = books[instrument_key]
        remaining = int(execution["quantity"])
        execution_charges_per_unit = (
            float(execution.get("total_charges", 0)) / remaining if remaining else 0.0
        )

        while remaining > 0 and book[opposite]:
            opening = book[opposite][0]
            matched = min(remaining, opening["quantity"])
            if opening["transaction_type"] == "BUY":
                pnl = (execution["price"] - opening["price"]) * matched
            else:
                pnl = (opening["price"] - execution["price"]) * matched
            closed_rows.append(_trade_row(
                execution=opening,
                quantity=matched,
                status="CLOSED",
                exit_order_id=execution["order_id"],
                exit_price=execution["price"],
                exit_time=execution["timestamp"],
                realized_pnl=round(pnl, 2),
                total_charges=round(
                    matched * (
                        float(opening.get("charges_per_unit", 0))
                        + execution_charges_per_unit
                    ),
                    4,
                ),
            ))
            opening["quantity"] -= matched
            remaining -= matched
            if opening["quantity"] == 0:
                book[opposite].popleft()

        if remaining > 0:
            opening = dict(execution)
            opening["quantity"] = remaining
            opening["charges_per_unit"] = execution_charges_per_unit
            book[side].append(opening)

    open_rows = []
    for book in books.values():
        for side in ("BUY", "SELL"):
            for opening in book[side]:
                open_rows.append(_trade_row(
                    execution=opening,
                    quantity=opening["quantity"],
                    status="OPEN",
                    total_charges=round(
                        opening["quantity"] * float(opening.get("charges_per_unit", 0)),
                        4,
                    ),
                ))
    return sorted(
        closed_rows + open_rows,
        key=lambda row: (row["entry_time"], row["entry_order_id"], row["status"]),
    )


def _trade_row(
    execution: dict,
    quantity: int,
    status: str,
    exit_order_id: str | None = None,
    exit_price: float | None = None,
    exit_time: datetime | None = None,
    realized_pnl: float | None = None,
    total_charges: float = 0.0,
) -> dict:
    tradingsymbol = execution["tradingsymbol"]
    return {
        "symbol": _extract_root_symbol(tradingsymbol),
        "tradingsymbol": tradingsymbol,
        "option_type": _parse_option_type(tradingsymbol),
        "entry_order_id": execution["order_id"],
        "exit_order_id": exit_order_id,
        "trade_type": execution["transaction_type"],
        "quantity": quantity,
        "entry_price": round(float(execution["price"]), 4),
        "exit_price": round(float(exit_price), 4) if exit_price is not None else None,
        "realized_pnl": realized_pnl,
        "total_charges": total_charges,
        "status": status,
        "entry_time": execution["timestamp"],
        "exit_time": exit_time,
    }


def _extract_root_symbol(tradingsymbol: str) -> str:
    match = re.match(r"^([A-Z\-]+)", tradingsymbol)
    return match.group(1) if match else tradingsymbol


def _parse_option_type(tradingsymbol: str) -> str:
    if tradingsymbol.endswith("CE"):
        return "CE"
    if tradingsymbol.endswith("PE"):
        return "PE"
    if tradingsymbol.endswith("FUT"):
        return "FUT"
    return "EQ"


def _signature(row: dict) -> tuple:
    return (
        row["tradingsymbol"], row["trade_type"], int(row["quantity"]),
        round(float(row["entry_price"]), 4),
        round(float(row["exit_price"]), 4) if row.get("exit_price") is not None else None,
        round(float(row["realized_pnl"]), 2) if row.get("realized_pnl") is not None else None,
        round(float(row.get("total_charges") or 0), 4),
        row["status"], str(row["entry_order_id"]),
        str(row["exit_order_id"]) if row.get("exit_order_id") else None,
        row["entry_time"].replace(tzinfo=None, microsecond=0),
        row["exit_time"].replace(tzinfo=None, microsecond=0) if row.get("exit_time") else None,
    )


def _row_identity(row: dict) -> tuple:
    """Stable execution identity used for FK-safe in-place corrections."""
    return (
        str(row["entry_order_id"]),
        str(row["exit_order_id"]) if row.get("exit_order_id") else None,
        str(row["tradingsymbol"]),
        str(row["trade_type"]),
        int(row["quantity"]),
        str(row["status"]),
    )


def signatures_match(actual_rows: Iterable[dict], expected_rows: Iterable[dict]) -> bool:
    """Compare duplicate-aware row signatures without ordering None and numbers."""
    return Counter(map(_signature, actual_rows)) == Counter(map(_signature, expected_rows))


def attach_order_charges(kite, order_executions: list[dict], raw_orders: Iterable[dict]) -> bool:
    """Attach official Kite all-in charge totals to aggregated order executions."""
    order_details = {str(order.get("order_id")): order for order in raw_orders}
    pending_payloads = []
    pending_executions = []

    for execution in order_executions:
        order = order_details.get(execution["order_id"], {})
        cache_key = (
            execution["order_id"], execution["quantity"],
            round(float(execution["price"]), 4),
        )
        if cache_key in _TOTAL_CHARGES_CACHE:
            execution["total_charges"] = _TOTAL_CHARGES_CACHE[cache_key]
            continue
        pending_payloads.append({
            "order_id": execution["order_id"],
            "exchange": execution["exchange"],
            "tradingsymbol": execution["tradingsymbol"],
            "transaction_type": execution["transaction_type"],
            "variety": order.get("variety") or "regular",
            "product": execution["product"],
            "order_type": order.get("order_type") or "MARKET",
            "quantity": execution["quantity"],
            "average_price": execution["price"],
        })
        pending_executions.append((execution, cache_key))

    if not pending_payloads:
        return True

    try:
        if hasattr(kite, "order_charges"):
            charge_rows = kite.order_charges(pending_payloads)
        else:
            # Compatibility for kiteconnect 5.0.1, whose route table predates
            # the public order_charges wrapper while the v3 backend supports it.
            kite._routes.setdefault("order.charges", "/charges/orders")
            charge_rows = kite._post(
                "order.charges", params=pending_payloads, is_json=True
            )
        for (execution, cache_key), charge_row in zip(pending_executions, charge_rows):
            total_charges = round(float(charge_row.get("charges", {}).get("total", 0)), 4)
            execution["total_charges"] = total_charges
            _TOTAL_CHARGES_CACHE[cache_key] = total_charges
        if len(charge_rows) != len(pending_executions):
            raise RuntimeError(
                f"Expected {len(pending_executions)} charge rows, received {len(charge_rows)}"
            )
        return True
    except Exception as exc:
        logger.error("Kite total-charge calculation failed; reconciliation deferred: %s", exc)
        return False


def compare_open_quantities(db_quantities: dict[str, int], broker_quantities: dict[str, int]) -> dict[str, tuple[int, int]]:
    """Return symbol -> (database quantity, broker quantity) mismatches."""
    mismatches = {}
    for symbol in set(db_quantities) | set(broker_quantities):
        db_quantity = int(db_quantities.get(symbol, 0))
        broker_quantity = int(broker_quantities.get(symbol, 0))
        if db_quantity != broker_quantity:
            mismatches[symbol] = (db_quantity, broker_quantity)
    return mismatches


def _row_open_quantities(rows: Iterable[dict]) -> dict[str, int]:
    quantities: dict[str, int] = defaultdict(int)
    for row in rows:
        if row["status"] != "OPEN":
            continue
        direction = 1 if row["trade_type"] == "BUY" else -1
        quantities[row["tradingsymbol"]] += direction * int(row["quantity"])
    return dict(quantities)


def _broker_position_quantities(positions: Iterable[dict]) -> dict[str, int]:
    quantities: dict[str, int] = defaultdict(int)
    for position in positions:
        quantities[str(position["tradingsymbol"])] += int(position.get("quantity", 0))
    return dict(quantities)


def audit_open_quantities(
    kite,
    db: Session,
    user_id: int,
    exchange_link_id: str,
) -> dict[str, tuple[int, int]]:
    """Validate the derived OPEN ledger against Kite's authoritative net positions."""
    db_rows = db.execute(text("""
        SELECT tradingsymbol,
               SUM(CASE WHEN trade_type = 'BUY' THEN quantity ELSE -quantity END) AS net_quantity
        FROM market_trades
        WHERE status = 'OPEN'
          AND user_id = :user_id
          AND exchange_link_id = CAST(:exchange_link_id AS UUID)
        GROUP BY tradingsymbol
    """), {
        "user_id": user_id,
        "exchange_link_id": exchange_link_id,
    }).fetchall()
    db_quantities = {row.tradingsymbol: int(row.net_quantity or 0) for row in db_rows}
    try:
        net_positions = kite.positions().get("net", [])
    except Exception as exc:
        logger.error("Kite position audit fetch failed: %s", exc)
        return {}
    broker_quantities = _broker_position_quantities(net_positions)

    mismatches = compare_open_quantities(db_quantities, broker_quantities)
    for symbol, (db_quantity, broker_quantity) in sorted(mismatches.items()):
        logger.critical(
            "[%s] OPEN QUANTITY MISMATCH: database=%s broker=%s",
            symbol, db_quantity, broker_quantity,
        )
    if not mismatches:
        logger.info("Open-quantity audit passed for %d symbols", len(set(db_quantities) | set(broker_quantities)))
    return mismatches


def reconcile_carryover_closures(
    db: Session,
    user_id: int,
    exchange_link_id: str,
    day_start: datetime,
    executions: Iterable[dict],
) -> set[str]:
    """Close exact carry-over lots with today's opposite-side Kite execution.

    The old start-of-day replay deliberately treated overnight positions as
    audit-only. That protected the original entry, but it also left a position
    opened yesterday as OPEN forever when it was exited today.  We only update
    an exact quantity match; partial or ambiguous inventory remains untouched
    and is logged for review rather than guessed.
    """
    rows = db.execute(text("""
        SELECT id, tradingsymbol, trade_type, quantity, entry_price, total_charges
        FROM market_trades
        WHERE user_id = :user_id
          AND exchange_link_id = CAST(:exchange_link_id AS UUID)
          AND status = 'OPEN'
          AND entry_time < :day_start
        ORDER BY entry_time, id
    """), {
        "user_id": user_id,
        "exchange_link_id": exchange_link_id,
        "day_start": day_start,
    }).mappings().all()
    books: dict[tuple[str, str], deque] = defaultdict(deque)
    for row in rows:
        books[(str(row["tradingsymbol"]), str(row["trade_type"]).upper())].append(dict(row))

    repaired: set[str] = set()
    for execution in executions:
        side = str(execution.get("transaction_type", "")).upper()
        if side not in {"BUY", "SELL"}:
            continue
        opening_side = "SELL" if side == "BUY" else "BUY"
        key = (str(execution["tradingsymbol"]), opening_side)
        if not books[key]:
            continue
        opening = books[key][0]
        exit_quantity = int(execution["quantity"])
        if int(opening["quantity"]) != exit_quantity:
            logger.warning(
                "[%s] Carry-over closure needs manual review: open qty=%s, exit qty=%s",
                key[0], opening["quantity"], exit_quantity,
            )
            continue
        entry_price = float(opening["entry_price"])
        exit_price = float(execution["price"])
        pnl_multiplier = 1 if opening_side == "BUY" else -1
        realized_pnl = round((exit_price - entry_price) * exit_quantity * pnl_multiplier, 2)
        db.execute(text("""
            UPDATE market_trades
            SET status = 'CLOSED', exit_order_id = :exit_order_id,
                exit_price = :exit_price, exit_time = :exit_time,
                realized_pnl = :realized_pnl,
                total_charges = COALESCE(total_charges, 0) + :exit_charges,
                updated_at = NOW()
            WHERE id = :id AND status = 'OPEN'
        """), {
            "id": opening["id"],
            "exit_order_id": execution["order_id"],
            "exit_price": exit_price,
            "exit_time": execution["timestamp"],
            "realized_pnl": realized_pnl,
            "exit_charges": float(execution.get("total_charges", 0)),
        })
        books[key].popleft()
        repaired.add(_extract_root_symbol(key[0]))
        logger.warning("[%s] Reconciled carry-over position closed today", key[0])
    return repaired


def repair_misattributed_carryover_closures(
    db: Session,
    user_id: int,
    exchange_link_id: str,
    day_start: datetime,
    expected_rows: Iterable[dict],
) -> set[str]:
    """Replace a stale carry-over match with the broker's same-day round trip.

    An exit must first be paired with today's executions.  Older code closed a
    stale historical OPEN row before doing that pairing, which is how a real
    ₹158.95 entry was displayed as the old row's ₹430 entry.  Matching the
    unique exit order lets us correct the existing row in place and preserve
    any replay foreign keys.
    """
    repaired: set[str] = set()
    update_query = text("""
        UPDATE market_trades
        SET symbol = :symbol, tradingsymbol = :tradingsymbol,
            option_type = :option_type, entry_order_id = :entry_order_id,
            trade_type = :trade_type, quantity = :quantity,
            entry_price = :entry_price, exit_price = :exit_price,
            realized_pnl = :realized_pnl, total_charges = :total_charges,
            status = :status, entry_time = :entry_time, exit_time = :exit_time,
            updated_at = NOW()
        WHERE user_id = :user_id
          AND exchange_link_id = CAST(:exchange_link_id AS UUID)
          AND status = 'CLOSED'
          AND entry_time < :day_start
          AND exit_time >= :day_start
          AND exit_order_id = :exit_order_id
    """)
    for row in expected_rows:
        if row["status"] != "CLOSED" or not row.get("exit_order_id"):
            continue
        result = db.execute(update_query, {
            **row,
            "user_id": user_id,
            "exchange_link_id": exchange_link_id,
            "day_start": day_start,
        })
        if result.rowcount:
            repaired.add(row["symbol"])
            logger.warning(
                "[%s] Corrected stale carry-over match from broker executions",
                row["tradingsymbol"],
            )
    return repaired


def reconcile_broker_day_pnl(
    db: Session,
    user_id: int,
    exchange_link_id: str,
    day_start: datetime,
    day_positions: Iterable[dict],
) -> set[str]:
    """Audit closed-symbol gross P&L against Kite without inventing trades.

    A broker position is an aggregate, not an execution.  Persisting a P&L
    difference as a synthetic ``market_trades`` row made the journal display a
    midnight entry, zero charges, a fabricated return, and an extra trade.  It
    was especially wrong for carry-over positions, whose broker day values do
    not describe the original entry execution.

    Detailed Kite executions and exact carry-over closures remain the only
    sources allowed to create or close journal trades.  This audit now removes
    legacy synthetic rows and logs discrepancies for reconciliation instead.
    """
    repaired: set[str] = set()
    day_key = day_start.strftime("%Y%m%d")
    for position in day_positions:
        symbol = str(position.get("tradingsymbol") or "")
        if not symbol:
            continue
        adjustment_key = f"kite-day-adjustment:{day_key}:{symbol}"
        existing_id = db.execute(text("""
            SELECT id FROM market_trades
            WHERE user_id = :user_id
              AND exchange_link_id = CAST(:exchange_link_id AS UUID)
              AND entry_order_id = :adjustment_key
            LIMIT 1
        """), {
            "user_id": user_id,
            "exchange_link_id": exchange_link_id,
            "adjustment_key": adjustment_key,
        }).scalar()
        if existing_id:
            db.execute(text("DELETE FROM market_trades WHERE id = :id"), {"id": existing_id})
            repaired.add(_extract_root_symbol(symbol))
            logger.warning("[%s] Removed legacy synthetic broker-day adjustment", symbol)

        # Preserve an explicit zero in Kite's day fields.  Falling back via
        # ``or`` incorrectly substituted carry-over totals for a zero day buy.
        buy_quantity = float(
            position["day_buy_quantity"]
            if position.get("day_buy_quantity") is not None
            else position.get("buy_quantity") or 0
        )
        sell_quantity = float(
            position["day_sell_quantity"]
            if position.get("day_sell_quantity") is not None
            else position.get("sell_quantity") or 0
        )
        buy_value = float(
            position["day_buy_value"]
            if position.get("day_buy_value") is not None
            else position.get("buy_value") or 0
        )
        sell_value = float(
            position["day_sell_value"]
            if position.get("day_sell_value") is not None
            else position.get("sell_value") or 0
        )
        if int(position.get("quantity") or 0) != 0:
            continue
        if buy_quantity <= 0 or sell_quantity <= 0 or buy_quantity != sell_quantity:
            continue

        broker_pnl = round(sell_value - buy_value, 2)
        journal_gross = db.execute(text("""
            SELECT COALESCE(SUM(realized_pnl), 0)
            FROM market_trades
            WHERE user_id = :user_id
              AND exchange_link_id = CAST(:exchange_link_id AS UUID)
              AND status = 'CLOSED'
              AND tradingsymbol = :symbol
              AND exit_time >= :day_start
              AND entry_order_id <> :adjustment_key
        """), {
            "user_id": user_id,
            "exchange_link_id": exchange_link_id,
            "symbol": symbol,
            "day_start": day_start,
            "adjustment_key": adjustment_key,
        }).scalar_one()
        difference = round(broker_pnl - float(journal_gross or 0), 2)
        if abs(difference) >= 0.01:
            logger.error(
                "[%s] Broker-day gross P&L differs from journal by %.2f; "
                "waiting for execution-level reconciliation",
                symbol,
                difference,
            )
    return repaired


def reconcile_trades_from_start_of_day(
    kite,
    db: Session,
    user_id: int,
    provider: str = "zerodha",
    *,
    correct_existing: bool = True,
) -> set[str]:
    """Audit and repair today's non-carryover trade rows from Kite's trade book.

    Kite's day trade book rolls over at midnight, while the Trading Desk keeps a
    session visible until 06:00 IST.  Do not reconcile during that gap: an empty
    broker day book must never replace the prior session's rows or charges.
    """
    if is_before_session_reset():
        logger.info("Trade reconciliation is paused until the 06:00 IST session reset")
        return set()

    attribution = resolve_trade_attribution(db, user_id, provider)
    if attribution is None:
        return set()
    trade_user_id, exchange_link_id = attribution
    today = datetime.now(IST).date()
    day_start = datetime.combine(today, time.min)

    attribution_columns = db.execute(text("""
        SELECT COUNT(*)
        FROM information_schema.columns
        WHERE table_name = 'market_trades'
          AND column_name IN ('user_id', 'exchange_link_id')
    """)).scalar_one()
    if attribution_columns != 2:
        logger.critical(
            "market_trades account attribution is missing; apply "
            "the Rails migration 20260929120000_add_account_attribution_to_market_trades"
        )
        return set()

    # Kite's positions endpoint is an aggregate audit signal.  Check it before
    # the execution feed and remove any legacy synthetic adjustment rows, but
    # never turn a position-level difference into a journal trade.
    day_positions = None
    repaired_broker_symbols: set[str] = set()
    try:
        day_positions = kite.positions().get("day", [])
        repaired_broker_symbols = reconcile_broker_day_pnl(
            db, trade_user_id, exchange_link_id, day_start, day_positions
        )
        if repaired_broker_symbols:
            db.commit()
    except Exception as exc:
        logger.error("Kite day-position reconciliation failed; execution reconciliation will continue: %s", exc)

    try:
        executions = kite.trades()
    except Exception as exc:
        logger.error("Kite execution fetch failed: %s", exc)
        return repaired_broker_symbols
    if not executions:
        audit_open_quantities(kite, db, trade_user_id, exchange_link_id)
        return repaired_broker_symbols

    total_charges_column_exists = db.execute(text("""
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = 'market_trades' AND column_name = 'total_charges'
        )
    """)).scalar_one()
    if not total_charges_column_exists:
        logger.critical(
            "market_trades.total_charges is missing; apply "
            "resources/add_market_trades_total_charges.sql"
        )
        return repaired_broker_symbols

    todays_executions = [e for e in executions if _execution_time(e).date() == today]
    order_executions = aggregate_order_executions(todays_executions)
    try:
        raw_orders = kite.orders()
    except Exception as exc:
        logger.error("Kite order-detail fetch failed; using default order metadata: %s", exc)
        raw_orders = []
    if not attach_order_charges(kite, order_executions, raw_orders):
        audit_open_quantities(kite, db, trade_user_id, exchange_link_id)
        return repaired_broker_symbols
    expected_rows = build_fifo_trade_rows(order_executions)
    if not expected_rows:
        audit_open_quantities(kite, db, trade_user_id, exchange_link_id)
        return repaired_broker_symbols

    repaired_misattributed_symbols = set()
    if correct_existing:
        repaired_misattributed_symbols = repair_misattributed_carryover_closures(
            db, trade_user_id, exchange_link_id, day_start, expected_rows
        )

    # Only executions left OPEN by today's FIFO replay can close overnight
    # inventory.  A sell already paired with today's buy must never consume an
    # older journal row.
    unmatched_order_ids = {
        row["entry_order_id"] for row in expected_rows if row["status"] == "OPEN"
    }
    unmatched_executions = [
        execution for execution in order_executions
        if execution["order_id"] in unmatched_order_ids
    ]
    repaired_carryover_symbols = reconcile_carryover_closures(
        db, trade_user_id, exchange_link_id, day_start, unmatched_executions
    )
    if repaired_misattributed_symbols or repaired_carryover_symbols:
        # Persist before any safe early return below; reconciliation can be
        # carry-over-only with no repairable same-day symbols.
        db.commit()
    repaired_execution_symbols = (
        repaired_misattributed_symbols
        | repaired_carryover_symbols
        | repaired_broker_symbols
    )

    all_symbols = {row["tradingsymbol"] for row in expected_rows}
    carryover_query = text("""
        SELECT DISTINCT tradingsymbol
        FROM market_trades
        WHERE entry_time < :day_start
          AND user_id = :user_id
          AND exchange_link_id = CAST(:exchange_link_id AS UUID)
          AND (status = 'OPEN' OR exit_time >= :day_start)
          AND tradingsymbol IN :symbols
    """).bindparams(bindparam("symbols", expanding=True))
    carryover_symbols = {
        row[0] for row in db.execute(
            carryover_query, {
                "day_start": day_start,
                "symbols": sorted(all_symbols),
                "user_id": trade_user_id,
                "exchange_link_id": exchange_link_id,
            }
        ).fetchall()
    }
    for symbol in sorted(carryover_symbols):
        logger.warning(
            "[%s] Reconciliation is audit-only: overnight opening inventory exists",
            symbol,
        )

    repairable = all_symbols - carryover_symbols
    if not repairable:
        audit_open_quantities(kite, db, trade_user_id, exchange_link_id)
        return repaired_execution_symbols

    if day_positions is None:
        try:
            day_positions = kite.positions().get("day", [])
        except Exception as exc:
            logger.error("Kite day-position validation failed; auto-repair skipped: %s", exc)
            return repaired_execution_symbols
    expected_day_quantities = _row_open_quantities(expected_rows)
    broker_day_quantities = _broker_position_quantities(day_positions)
    unsafe_symbols = {
        symbol for symbol in repairable
        if int(expected_day_quantities.get(symbol, 0)) != int(broker_day_quantities.get(symbol, 0))
    }
    for symbol in sorted(unsafe_symbols):
        logger.critical(
            "[%s] Auto-repair skipped: replayed day quantity=%s, broker day quantity=%s",
            symbol,
            expected_day_quantities.get(symbol, 0),
            broker_day_quantities.get(symbol, 0),
        )
    repairable -= unsafe_symbols
    if not repairable:
        audit_open_quantities(kite, db, trade_user_id, exchange_link_id)
        return repaired_execution_symbols

    actual_query = text("""
        SELECT id, tradingsymbol, trade_type, quantity, entry_price, exit_price,
               realized_pnl, total_charges, status, entry_order_id, exit_order_id,
               entry_time, exit_time
        FROM market_trades
        WHERE entry_time >= :day_start
          AND user_id = :user_id
          AND exchange_link_id = CAST(:exchange_link_id AS UUID)
          AND entry_order_id NOT LIKE 'kite-day-adjustment:%'
          AND tradingsymbol IN :symbols
    """).bindparams(bindparam("symbols", expanding=True))
    actual_rows = [dict(row._mapping) for row in db.execute(
        actual_query, {
            "day_start": day_start,
            "symbols": sorted(repairable),
            "user_id": trade_user_id,
            "exchange_link_id": exchange_link_id,
        }
    ).fetchall()]
    expected_repairable = [row for row in expected_rows if row["tradingsymbol"] in repairable]

    if signatures_match(actual_rows, expected_repairable):
        logger.info("Trade reconciliation passed for %d symbols", len(repairable))
        audit_open_quantities(kite, db, trade_user_id, exchange_link_id)
        return repaired_execution_symbols

    # The one-minute pass is append-focused: it may add a wholly new symbol,
    # but leaves any existing journal correction to the ten-minute audit.
    if actual_rows and not correct_existing:
        logger.info("Existing journal differences deferred to ten-minute reconciliation")
        audit_open_quantities(kite, db, trade_user_id, exchange_link_id)
        return repaired_execution_symbols

    # When the execution identities still match, correct mutable values in
    # place.  This preserves market_trade IDs used by journal replays and lets
    # periodic reconciliation repair an initially stale/partial average price.
    actual_by_identity = {_row_identity(row): row for row in actual_rows}
    expected_by_identity = {_row_identity(row): row for row in expected_repairable}
    identities_are_unique = (
        len(actual_by_identity) == len(actual_rows)
        and len(expected_by_identity) == len(expected_repairable)
    )
    if identities_are_unique and actual_by_identity.keys() == expected_by_identity.keys():
        update_query = text("""
            UPDATE market_trades
            SET entry_price = :entry_price, exit_price = :exit_price,
                realized_pnl = :realized_pnl, total_charges = :total_charges,
                entry_time = :entry_time, exit_time = :exit_time,
                updated_at = NOW()
            WHERE id = :id
        """)
        for identity, expected in expected_by_identity.items():
            db.execute(update_query, {
                "id": actual_by_identity[identity]["id"],
                "entry_price": expected["entry_price"],
                "exit_price": expected.get("exit_price"),
                "realized_pnl": expected.get("realized_pnl"),
                "total_charges": expected.get("total_charges", 0),
                "entry_time": expected["entry_time"],
                "exit_time": expected.get("exit_time"),
            })
        db.commit()
        corrected_symbols = {row["symbol"] for row in expected_repairable}
        logger.warning(
            "Trade reconciliation corrected %d journal rows in place",
            len(expected_repairable),
        )
        audit_open_quantities(kite, db, trade_user_id, exchange_link_id)
        return repaired_execution_symbols | corrected_symbols

    dependent_fk_count = db.execute(text("""
        SELECT COUNT(*)
        FROM information_schema.table_constraints tc
        JOIN information_schema.constraint_column_usage ccu
          ON ccu.constraint_name = tc.constraint_name
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND ccu.table_name = 'market_trades'
          AND tc.table_name <> 'market_trades'
    """)).scalar_one()
    if dependent_fk_count and actual_rows:
        logger.critical(
            "Trade reconciliation found a mismatch but auto-repair was skipped: "
            "market_trades has %s dependent foreign keys",
            dependent_fk_count,
        )
        audit_open_quantities(kite, db, trade_user_id, exchange_link_id)
        return repaired_execution_symbols

    delete_query = text("""
        DELETE FROM market_trades
        WHERE entry_time >= :day_start
          AND user_id = :user_id
          AND exchange_link_id = CAST(:exchange_link_id AS UUID)
          AND entry_order_id NOT LIKE 'kite-day-adjustment:%'
          AND tradingsymbol IN :symbols
    """).bindparams(bindparam("symbols", expanding=True))
    db.execute(delete_query, {
        "day_start": day_start,
        "symbols": sorted(repairable),
        "user_id": trade_user_id,
        "exchange_link_id": exchange_link_id,
    })

    insert_query = text("""
        INSERT INTO market_trades (
            user_id, exchange_link_id,
            symbol, tradingsymbol, option_type, entry_order_id, exit_order_id,
            trade_type, quantity, entry_price, exit_price, realized_pnl, total_charges,
            status, entry_time, exit_time, created_at, updated_at
        ) VALUES (
            :user_id, CAST(:exchange_link_id AS UUID),
            :symbol, :tradingsymbol, :option_type, :entry_order_id, :exit_order_id,
            :trade_type, :quantity, :entry_price, :exit_price, :realized_pnl, :total_charges,
            :status, :entry_time, :exit_time, NOW(), NOW()
        )
    """)
    for row in expected_repairable:
        db.execute(insert_query, {
            **row,
            "user_id": trade_user_id,
            "exchange_link_id": exchange_link_id,
        })
    db.commit()

    root_symbols = repaired_misattributed_symbols | repaired_carryover_symbols | repaired_broker_symbols | {row["symbol"] for row in expected_repairable}
    logger.warning(
        "Trade reconciliation repaired %d rows across %d symbols",
        len(expected_repairable), len(repairable),
    )
    audit_open_quantities(kite, db, trade_user_id, exchange_link_id)
    return root_symbols


class TradeReconciliationScheduler:
    def __init__(
        self,
        interval_seconds: float = 60.0,
        reconciliation_interval_seconds: float = 600.0,
    ):
        self.interval_seconds = interval_seconds
        self.reconciliation_interval_seconds = reconciliation_interval_seconds
        self._last_run = 0.0
        self._last_reconciliation = 0.0

    def run_if_due(self, kite, db: Session, user_id: int) -> set[str]:
        now = monotonic_time.monotonic()
        if now - self._last_run < self.interval_seconds:
            return set()
        full_reconciliation = (
            now - self._last_reconciliation >= self.reconciliation_interval_seconds
        )
        # Set before the network call to avoid a rapid failure loop.
        self._last_run = now
        if full_reconciliation:
            self._last_reconciliation = now
        return reconcile_trades_from_start_of_day(
            kite,
            db,
            user_id,
            correct_existing=full_reconciliation,
        )
