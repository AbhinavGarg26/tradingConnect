"""
kite_csv_import.py — import historical trades from Kite Console CSV export.

Confirmed CSV format (tradebook-UTE930-FO.csv):
    symbol, isin, trade_date, exchange, segment, series, trade_type,
    auction, quantity, price, trade_id, order_id, order_execution_time, expiry_date

P&L matching logic:
    For each symbol, matches BUY rows with SELL rows by date proximity.
    If BUY qty == SELL qty → matched trade, P&L = (sell_avg - buy_avg) × qty
    If unmatched → open position

Usage:
    python kite_csv_import.py --file tradebook-UTE930-FO.csv
    python kite_csv_import.py --file tradebook-UTE930-FO.csv --dry-run
    python kite_csv_import.py --file tradebook-UTE930-FO.csv --from 2026-03-01 --to 2026-03-22
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import select

from trading.database import get_db
from trading.models import Instrument, OrderEvent, Trade
from trading.repositories import UserRepo

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("kite_csv_import")


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def to_dec(value, default=Decimal("0")) -> Decimal:
    try:
        cleaned = str(value).replace(",", "").strip()
        return Decimal(cleaned) if cleaned else default
    except InvalidOperation:
        return default


def parse_dt(value: str) -> Optional[datetime]:
    for fmt in ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%d-%m-%Y %H:%M:%S"]:
        try:
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def parse_date(value: str) -> Optional[date]:
    for fmt in ["%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"]:
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def detect_trade_type(symbol: str, segment: str) -> str:
    seg = segment.strip().upper()
    if seg == "FO":
        sym = symbol.upper()
        return "OPTIONS" if sym.endswith(("CE", "PE")) else "FUTURES"
    return "EQUITY"


def detect_instrument_type(symbol: str) -> str:
    sym = symbol.upper()
    if sym.endswith("CE"):  return "CE"
    if sym.endswith("PE"):  return "PE"
    if "FUT" in sym:        return "FUT"
    return "EQ"


def mark_expired_instruments(db) -> int:
    """Mark all instruments with past expiry_date as inactive."""
    from sqlalchemy import update
    today = date.today()
    result = db.execute(
        update(Instrument)
        .where(
            Instrument.expiry_date != None,
            Instrument.expiry_date <  today,
            Instrument.is_active   == True,
        )
        .values(is_active=False)
    )
    return result.rowcount


def find_or_create_instrument(db, symbol, exchange, segment, expiry_str) -> Optional[object]:
    inst = db.scalar(
        select(Instrument).where(
            Instrument.symbol   == symbol,
            Instrument.exchange == exchange,
        )
    )
    if inst:
        return inst

    seg_map  = {"FO": "FO", "EQ": "EQ", "MCX": "FUT", "CDS": "FUT"}
    seg_norm = seg_map.get(segment.strip().upper(), "EQ")
    expiry   = parse_date(expiry_str) if expiry_str.strip() else None

    # Unique negative placeholder — real Kite tokens are always positive
    placeholder = -(abs(hash(f"{symbol}_{exchange}_{expiry_str}")) % 999_999_999)

    # Ensure no collision
    taken = db.scalar(select(Instrument).where(Instrument.instrument_token == placeholder))
    if taken:
        placeholder = -(abs(hash(f"{symbol}_{exchange}_{expiry_str}_2")) % 999_999_999)

    today = date.today()
    expiry = parse_date(expiry_str) if expiry_str.strip() else None
    is_active = (expiry is None or expiry >= today)

    inst = Instrument(
        symbol           = symbol,
        exchange         = exchange,
        segment          = seg_norm,
        instrument_type  = detect_instrument_type(symbol),
        instrument_token = placeholder,
        lot_size         = 1,
        tick_size        = Decimal("0.05"),
        expiry_date      = expiry,
        is_active        = is_active,
    )
    db.add(inst)
    db.flush()
    logger.info("Auto-created instrument: %s [%s] token=%s expiry=%s", symbol, exchange, placeholder, expiry)
    return inst


# ─────────────────────────────────────────────────────────────
# P&L matching logic
# ─────────────────────────────────────────────────────────────

def weighted_avg(rows: list[dict]) -> tuple[Decimal, Decimal]:
    """Returns (total_qty, weighted_avg_price) from a list of fill rows."""
    total_qty = sum(to_dec(r.get("quantity", "0")) for r in rows)
    total_val = sum(to_dec(r.get("quantity", "0")) * to_dec(r.get("price", "0")) for r in rows)
    avg = (total_val / total_qty).quantize(Decimal("0.05")) if total_qty else Decimal("0")
    return total_qty, avg


def match_trades_for_symbol(
    symbol: str,
    exchange: str,
    all_rows: list[dict],
) -> list[dict]:
    """
    Given all fills for a symbol (any direction, any date),
    match BUY and SELL sides into completed trades.

    Returns a list of trade dicts:
        {
            entry_direction: BUY or SELL,
            entry_rows:      [...],
            exit_rows:       [...] or [],
            entry_ts:        datetime,
            exit_ts:         datetime or None,
            entry_price:     Decimal,
            exit_price:      Decimal or None,
            quantity:        Decimal,
            pnl:             Decimal or None,
            status:          'closed' or 'open',
        }
    """
    # Separate and sort by time
    buys  = sorted([r for r in all_rows if r["trade_type"].strip().lower() == "buy"],
                   key=lambda r: r.get("order_execution_time",""))
    sells = sorted([r for r in all_rows if r["trade_type"].strip().lower() == "sell"],
                   key=lambda r: r.get("order_execution_time",""))

    buy_qty,  buy_avg  = weighted_avg(buys)
    sell_qty, sell_avg = weighted_avg(sells)

    trades = []

    if buy_qty > Decimal("0") and sell_qty > Decimal("0"):
        matched_qty = min(buy_qty, sell_qty)

        entry_ts = parse_dt(buys[0].get("order_execution_time",""))  or datetime.now(timezone.utc)
        exit_ts  = parse_dt(sells[-1].get("order_execution_time","")) or datetime.now(timezone.utc)

        # Determine direction: whichever came first is the entry
        if entry_ts <= exit_ts:
            # BUY first → long trade
            pnl = (sell_avg - buy_avg) * matched_qty
            trades.append({
                "entry_direction": "BUY",
                "entry_rows":      buys,
                "exit_rows":       sells,
                "entry_ts":        entry_ts,
                "exit_ts":         exit_ts,
                "entry_price":     buy_avg,
                "exit_price":      sell_avg,
                "quantity":        matched_qty,
                "pnl":             pnl.quantize(Decimal("0.01")),
                "status":          "closed",
            })
        else:
            # SELL first → short trade
            pnl = (sell_avg - buy_avg) * matched_qty
            trades.append({
                "entry_direction": "SELL",
                "entry_rows":      sells,
                "exit_rows":       buys,
                "entry_ts":        exit_ts,   # sell was first
                "exit_ts":         entry_ts,  # buy was exit
                "entry_price":     sell_avg,
                "exit_price":      buy_avg,
                "quantity":        matched_qty,
                "pnl":             pnl.quantize(Decimal("0.01")),
                "status":          "closed",
            })

        # Unmatched qty = open position
        unmatched = buy_qty - sell_qty
        if unmatched > Decimal("0"):
            trades.append({
                "entry_direction": "BUY",
                "entry_rows":      buys,
                "exit_rows":       [],
                "entry_ts":        entry_ts,
                "exit_ts":         None,
                "entry_price":     buy_avg,
                "exit_price":      None,
                "quantity":        unmatched,
                "pnl":             None,
                "status":          "open",
            })
        elif unmatched < Decimal("0"):
            trades.append({
                "entry_direction": "SELL",
                "entry_rows":      sells,
                "exit_rows":       [],
                "entry_ts":        parse_dt(sells[0].get("order_execution_time","")) or datetime.now(timezone.utc),
                "exit_ts":         None,
                "entry_price":     sell_avg,
                "exit_price":      None,
                "quantity":        abs(unmatched),
                "pnl":             None,
                "status":          "open",
            })

    elif buy_qty > Decimal("0"):
        # Only buys — open long
        entry_ts = parse_dt(buys[0].get("order_execution_time","")) or datetime.now(timezone.utc)
        trades.append({
            "entry_direction": "BUY",
            "entry_rows":      buys,
            "exit_rows":       [],
            "entry_ts":        entry_ts,
            "exit_ts":         None,
            "entry_price":     buy_avg,
            "exit_price":      None,
            "quantity":        buy_qty,
            "pnl":             None,
            "status":          "open",
        })

    elif sell_qty > Decimal("0"):
        # Only sells — open short
        entry_ts = parse_dt(sells[0].get("order_execution_time","")) or datetime.now(timezone.utc)
        trades.append({
            "entry_direction": "SELL",
            "entry_rows":      sells,
            "exit_rows":       [],
            "entry_ts":        entry_ts,
            "exit_ts":         None,
            "entry_price":     sell_avg,
            "exit_price":      None,
            "quantity":        sell_qty,
            "pnl":             None,
            "status":          "open",
        })

    return trades


# ─────────────────────────────────────────────────────────────
# Core import
# ─────────────────────────────────────────────────────────────

def import_csv(filepath: Path, from_date: Optional[date], to_date: Optional[date], dry_run: bool) -> None:

    with open(filepath, newline="", encoding="utf-8-sig") as f:
        reader  = csv.DictReader(f)
        rows    = [{k.strip(): (v or "").strip() for k, v in row.items()} for row in reader]

    logger.info("CSV loaded — %d total rows", len(rows))

    # Filter by date
    filtered, skipped = [], 0
    for row in rows:
        td = parse_date(row.get("trade_date", ""))
        if td is None:
            continue
        if from_date and td < from_date:
            skipped += 1; continue
        if to_date and td > to_date:
            skipped += 1; continue
        filtered.append(row)

    logger.info("%d rows in range | %d skipped", len(filtered), skipped)

    # Group all rows by (symbol, exchange) regardless of direction
    # This allows proper BUY/SELL matching per instrument
    by_instrument: dict = defaultdict(list)
    for row in filtered:
        symbol   = row.get("symbol", "").strip().upper()
        exchange = row.get("exchange", "NSE").strip().upper()
        if symbol:
            by_instrument[(symbol, exchange)].append(row)

    logger.info("%d unique instruments found", len(by_instrument))

    stats = {
        "new_trades": 0, "skipped_dupes": 0,
        "new_orders": 0, "flagged": 0, "errors": 0,
        "winners": 0, "losers": 0, "total_pnl": Decimal("0"),
    }
    flagged: list[str] = []

    if dry_run:
        print(f"\n{'Symbol':<30} {'Dir':<6} {'Entry':>10} {'Exit':>10} {'Qty':>10} {'P&L':>12} {'Status'}")
        print("-" * 85)
        for (symbol, exchange), rows_for_inst in by_instrument.items():
            trades = match_trades_for_symbol(symbol, exchange, rows_for_inst)
            for t in trades:
                pnl_str = f"₹{t['pnl']:>10.2f}" if t["pnl"] is not None else "—"
                print(
                    f"{symbol:<30} {t['entry_direction']:<6} "
                    f"₹{t['entry_price']:>8.2f} "
                    f"{'₹'+str(t['exit_price'].quantize(Decimal('0.01'))) if t['exit_price'] else '—':>10} "
                    f"{t['quantity']:>10.0f} "
                    f"{pnl_str:>12} "
                    f"{t['status']}"
                )
                stats["new_trades"] += 1
                if t["pnl"] is not None:
                    stats["total_pnl"] += t["pnl"]
                    if t["pnl"] > 0: stats["winners"] += 1
                    elif t["pnl"] < 0: stats["losers"] += 1
    else:
        # Load user_id once
        with get_db() as db:
            user = UserRepo.get_active(db)
            if not user:
                logger.critical("No active user in DB"); sys.exit(1)
            user_id = user.id

        # Each instrument gets its own session
        for (symbol, exchange), rows_for_inst in by_instrument.items():
            try:
                trades = match_trades_for_symbol(symbol, exchange, rows_for_inst)
                for t in trades:
                    with get_db() as db:
                        _save_trade(db, user_id, symbol, exchange,
                                    rows_for_inst[0], t, stats, flagged)
                        if t["pnl"] is not None:
                            stats["total_pnl"] += t["pnl"]
                            if t["pnl"] > 0: stats["winners"] += 1
                            elif t["pnl"] < 0: stats["losers"] += 1
            except Exception as exc:
                logger.error("Error %s [%s]: %s", symbol, exchange, exc, exc_info=True)
                stats["errors"] += 1

    # Summary
    win_rate = 0.0
    closed = stats["winners"] + stats["losers"]
    if closed > 0:
        win_rate = round(stats["winners"] / closed * 100, 1)

    print("\n" + "=" * 60)
    print(f"IMPORT {'(DRY RUN) ' if dry_run else ''}COMPLETE")
    print(f"  Trades imported  : {stats['new_trades']}")
    print(f"  Order events     : {stats['new_orders']}")
    print(f"  Dupes skipped    : {stats['skipped_dupes']}")
    print(f"  Winners          : {stats['winners']}")
    print(f"  Losers           : {stats['losers']}")
    print(f"  Win rate         : {win_rate}%")
    print(f"  Total P&L        : ₹{stats['total_pnl']:,.2f}")
    print(f"  Flagged          : {stats['flagged']}")
    print(f"  Errors           : {stats['errors']}")
    print("=" * 60)

    if flagged:
        print("\nFlagged — review manually:")
        for item in flagged:
            print(f"  • {item}")

    # After all trades imported — mark expired instruments
    if not dry_run:
        with get_db() as db:
            expired_count = mark_expired_instruments(db)
            if expired_count > 0:
                logger.info("Marked %d expired instruments as inactive", expired_count)
            print(f"  Expired instruments deactivated: {expired_count}")


def _save_trade(db, user_id, symbol, exchange, sample_row, t: dict, stats: dict, flagged: list):
    segment    = sample_row.get("segment", "EQ")
    expiry_str = sample_row.get("expiry_date", "")

    inst = find_or_create_instrument(db, symbol, exchange, segment, expiry_str)
    if not inst:
        flagged.append(f"Could not create instrument: {symbol} [{exchange}]")
        stats["flagged"] += 1
        return

    # Check for existing trade — match on instrument + direction + entered_at date
    entry_date = t["entry_ts"].date()
    day_start  = datetime.combine(entry_date, datetime.min.time()).replace(tzinfo=timezone.utc)
    day_end    = day_start + timedelta(days=1)

    existing = db.scalar(
        select(Trade).where(
            Trade.instrument_id == inst.id,
            Trade.user_id       == user_id,
            Trade.direction     == t["entry_direction"],
            Trade.entered_at    >= day_start,
            Trade.entered_at    <  day_end,
        )
    )
    if existing:
        stats["skipped_dupes"] += 1
        logger.debug("Dupe skipped: %s %s %s", t["entry_direction"], symbol, entry_date)
        return

    trade_type = detect_trade_type(symbol, segment)
    status     = t["status"]
    pnl        = t["pnl"]

    trade = Trade(
        instrument_id = inst.id,
        user_id       = user_id,
        direction     = t["entry_direction"],
        trade_type    = trade_type,
        product       = "NRML",
        entry_price   = t["entry_price"],
        quantity      = t["quantity"],
        entry_value   = t["entry_price"] * t["quantity"],
        initial_sl    = t["entry_price"],   # placeholder — edit via Rails UI
        current_sl    = t["entry_price"],
        risk_amount   = Decimal("0"),
        sl_method     = "manual",
        status        = status,
        entered_at    = t["entry_ts"],
        exited_at     = t["exit_ts"] if status == "closed" else None,
        exit_reason   = "synced_from_csv" if status == "closed" else None,
        exit_price    = t["exit_price"],
        exit_value    = (t["exit_price"] * t["quantity"]) if t["exit_price"] else None,
        pnl           = pnl,
        pnl_pct       = ((pnl / (t["entry_price"] * t["quantity"])) * 100).quantize(Decimal("0.0001"))
                        if pnl and t["entry_price"] * t["quantity"] != 0 else None,
    )
    db.add(trade)
    db.flush()
    stats["new_trades"] += 1

    pnl_str = f"P&L=₹{pnl:.2f}" if pnl is not None else "open"
    logger.info(
        "Saved: %s %s @ ₹%s → ₹%s qty=%s %s",
        t["entry_direction"], symbol,
        t["entry_price"], t["exit_price"] or "—",
        t["quantity"], pnl_str,
    )

    # Save all fill rows as order events
    now = datetime.now(timezone.utc)
    all_fills = t["entry_rows"] + t["exit_rows"]
    for row in all_fills:
        order_id  = row.get("order_id",  "").strip()
        trade_id  = row.get("trade_id",  "").strip()
        exec_time = parse_dt(row.get("order_execution_time","")) or now
        kite_id   = order_id or trade_id or f"CSV_{symbol}_{exec_time.isoformat()}"

        exists = db.scalar(select(OrderEvent).where(OrderEvent.order_id == kite_id))
        if exists:
            stats["skipped_dupes"] += 1
            continue

        direction = row.get("trade_type","").strip().upper()
        if direction == "BUY": direction = "BUY"
        elif direction == "SELL": direction = "SELL"

        db.add(OrderEvent(
            trade_id          = trade.id,
            order_id     = kite_id,
            order_type        = "MARKET",
            transaction_type  = direction,
            variety           = "regular",
            status            = "COMPLETE",
            price             = to_dec(row.get("price","0")),
            quantity          = to_dec(row.get("quantity","0")),
            filled_quantity   = to_dec(row.get("quantity","0")),
            average_price     = to_dec(row.get("price","0")),
            exchange_order_id = order_id or None,
            placed_at         = exec_time,
            updated_at        = exec_time,
            created_at        = exec_time,
        ))
        stats["new_orders"] += 1


# ─────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Import Kite CSV trade book into DB")
    parser.add_argument("--file",    required=True,    help="Path to Kite CSV file")
    parser.add_argument("--from",    dest="from_date", default=None, help="Start date YYYY-MM-DD")
    parser.add_argument("--to",      dest="to_date",   default=None, help="End date YYYY-MM-DD")
    parser.add_argument("--dry-run", dest="dry_run",   action="store_true")
    parser.add_argument("--cleanup-expired", dest="cleanup_expired", action="store_true",
                        help="Mark all expired instruments inactive and exit")
    args = parser.parse_args()

    filepath = Path(args.file)
    if not filepath.exists():
        logger.critical("File not found: %s", filepath); sys.exit(1)

    from_date = date.fromisoformat(args.from_date) if args.from_date else None
    to_date   = date.fromisoformat(args.to_date)   if args.to_date   else None

    logger.info("=" * 60)
    logger.info("Kite CSV import | file=%s | dry_run=%s", filepath.name, args.dry_run)
    if from_date: logger.info("From : %s", from_date)
    if to_date:   logger.info("To   : %s", to_date)
    logger.info("=" * 60)

    if args.cleanup_expired:
        with get_db() as db:
            count = mark_expired_instruments(db)
        print(f"Marked {count} expired instruments as inactive")
        sys.exit(0)

    import_csv(filepath, from_date, to_date, args.dry_run)


if __name__ == "__main__":
    main()