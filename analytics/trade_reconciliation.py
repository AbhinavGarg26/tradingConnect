"""Deterministic start-of-day trade reconciliation using Kite executions."""

from __future__ import annotations

from collections import defaultdict, deque
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
_TOTAL_CHARGES_CACHE: dict[tuple, float] = {}


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


def audit_open_quantities(kite, db: Session) -> dict[str, tuple[int, int]]:
    """Validate the derived OPEN ledger against Kite's authoritative net positions."""
    db_rows = db.execute(text("""
        SELECT tradingsymbol,
               SUM(CASE WHEN trade_type = 'BUY' THEN quantity ELSE -quantity END) AS net_quantity
        FROM market_trades
        WHERE status = 'OPEN'
        GROUP BY tradingsymbol
    """)).fetchall()
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


def reconcile_trades_from_start_of_day(kite, db: Session) -> set[str]:
    """Audit and repair today's non-carryover trade rows from Kite's trade book."""
    try:
        executions = kite.trades()
    except Exception as exc:
        logger.error("Kite execution fetch failed: %s", exc)
        return set()
    if not executions:
        audit_open_quantities(kite, db)
        return set()

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
        return set()

    today = datetime.now(IST).date()
    todays_executions = [e for e in executions if _execution_time(e).date() == today]
    order_executions = aggregate_order_executions(todays_executions)
    try:
        raw_orders = kite.orders()
    except Exception as exc:
        logger.error("Kite order-detail fetch failed; using default order metadata: %s", exc)
        raw_orders = []
    if not attach_order_charges(kite, order_executions, raw_orders):
        audit_open_quantities(kite, db)
        return set()
    expected_rows = build_fifo_trade_rows(order_executions)
    if not expected_rows:
        audit_open_quantities(kite, db)
        return set()

    all_symbols = {row["tradingsymbol"] for row in expected_rows}
    day_start = datetime.combine(today, time.min)
    carryover_query = text("""
        SELECT DISTINCT tradingsymbol
        FROM market_trades
        WHERE entry_time < :day_start
          AND (status = 'OPEN' OR exit_time >= :day_start)
          AND tradingsymbol IN :symbols
    """).bindparams(bindparam("symbols", expanding=True))
    carryover_symbols = {
        row[0] for row in db.execute(
            carryover_query, {"day_start": day_start, "symbols": sorted(all_symbols)}
        ).fetchall()
    }
    for symbol in sorted(carryover_symbols):
        logger.warning(
            "[%s] Reconciliation is audit-only: overnight opening inventory exists",
            symbol,
        )

    repairable = all_symbols - carryover_symbols
    if not repairable:
        audit_open_quantities(kite, db)
        return set()

    try:
        day_positions = kite.positions().get("day", [])
    except Exception as exc:
        logger.error("Kite day-position validation failed; auto-repair skipped: %s", exc)
        return set()
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
        audit_open_quantities(kite, db)
        return set()

    actual_query = text("""
        SELECT tradingsymbol, trade_type, quantity, entry_price, exit_price,
               realized_pnl, total_charges, status, entry_order_id, exit_order_id,
               entry_time, exit_time
        FROM market_trades
        WHERE entry_time >= :day_start AND tradingsymbol IN :symbols
    """).bindparams(bindparam("symbols", expanding=True))
    actual_rows = [dict(row._mapping) for row in db.execute(
        actual_query, {"day_start": day_start, "symbols": sorted(repairable)}
    ).fetchall()]
    expected_repairable = [row for row in expected_rows if row["tradingsymbol"] in repairable]

    if sorted(map(_signature, actual_rows)) == sorted(map(_signature, expected_repairable)):
        logger.info("Trade reconciliation passed for %d symbols", len(repairable))
        audit_open_quantities(kite, db)
        return set()

    dependent_fk_count = db.execute(text("""
        SELECT COUNT(*)
        FROM information_schema.table_constraints tc
        JOIN information_schema.constraint_column_usage ccu
          ON ccu.constraint_name = tc.constraint_name
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND ccu.table_name = 'market_trades'
    """)).scalar_one()
    if dependent_fk_count:
        logger.critical(
            "Trade reconciliation found a mismatch but auto-repair was skipped: "
            "market_trades has %s dependent foreign keys",
            dependent_fk_count,
        )
        audit_open_quantities(kite, db)
        return set()

    delete_query = text("""
        DELETE FROM market_trades
        WHERE entry_time >= :day_start AND tradingsymbol IN :symbols
    """).bindparams(bindparam("symbols", expanding=True))
    db.execute(delete_query, {"day_start": day_start, "symbols": sorted(repairable)})

    insert_query = text("""
        INSERT INTO market_trades (
            symbol, tradingsymbol, option_type, entry_order_id, exit_order_id,
            trade_type, quantity, entry_price, exit_price, realized_pnl, total_charges,
            status, entry_time, exit_time, created_at, updated_at
        ) VALUES (
            :symbol, :tradingsymbol, :option_type, :entry_order_id, :exit_order_id,
            :trade_type, :quantity, :entry_price, :exit_price, :realized_pnl, :total_charges,
            :status, :entry_time, :exit_time, NOW(), NOW()
        )
    """)
    for row in expected_repairable:
        db.execute(insert_query, row)
    db.commit()

    root_symbols = {row["symbol"] for row in expected_repairable}
    logger.warning(
        "Trade reconciliation repaired %d rows across %d symbols",
        len(expected_repairable), len(repairable),
    )
    audit_open_quantities(kite, db)
    return root_symbols


class TradeReconciliationScheduler:
    def __init__(self, interval_seconds: float = 30.0):
        self.interval_seconds = interval_seconds
        self._last_run = 0.0

    def run_if_due(self, kite, db: Session) -> set[str]:
        now = monotonic_time.monotonic()
        if now - self._last_run < self.interval_seconds:
            return set()
        # Set before the network call to avoid a rapid failure loop.
        self._last_run = now
        return reconcile_trades_from_start_of_day(kite, db)
