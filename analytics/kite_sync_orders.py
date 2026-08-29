import re
import time
import logging
from datetime import datetime, date, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import text


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("MarketAnalytics")


def extract_root_symbol(tradingsymbol: str) -> str:
    """
    Dynamically extracts the underlying asset symbol from any contract.
    Examples:
      'SENSEX2682077100CE' -> 'SENSEX'
      'NIFTY26AUG24000CE'  -> 'NIFTY'
      'BANKNIFTY26AUG45000PE' -> 'BANKNIFTY'
      'RELIANCE'           -> 'RELIANCE'
    """
    match = re.match(r"^([A-Z\-]+)", tradingsymbol)
    return match.group(1) if match else tradingsymbol


def parse_option_type(tradingsymbol: str) -> str:
    if tradingsymbol.endswith("CE"):
        return "CE"
    elif tradingsymbol.endswith("PE"):
        return "PE"
    elif tradingsymbol.endswith("FUT"):
        return "FUT"
    return "EQ"


def process_and_merge_trades(kite, db: Session) -> set:
    """
    1. Pre-fetches processed order IDs from DB.
    2. Includes partially filled orders that were subsequently CANCELLED or REJECTED.
    3. Handles split entries/exits dynamically during FIFO matching.
    """
    processed_symbols = set()

    # Step 1: Pre-fetch processed order IDs from PostgreSQL
    existing_orders_query = text("""
        SELECT entry_order_id, exit_order_id 
        FROM market_trades 
        WHERE entry_time >= CURRENT_DATE - INTERVAL '1 day'
    """)
    rows = db.execute(existing_orders_query).fetchall()

    processed_order_ids = set()
    for row in rows:
        if row.entry_order_id:
            processed_order_ids.add(str(row.entry_order_id))
        if row.exit_order_id:
            processed_order_ids.add(str(row.exit_order_id))

    # Step 2: Fetch raw orders from Kite API
    try:
        raw_orders = kite.orders()
    except Exception as e:
        logger.error(f"Kite order fetch error: {e}")
        return processed_symbols

    if not raw_orders:
        return processed_symbols

    # ACCEPTS BOTH 'COMPLETE' AND PARTIALLY FILLED 'CANCELLED' / 'REJECTED' ORDERS
    filled_orders = [
        o for o in raw_orders
        if int(o.get("filled_quantity", 0)) > 0
    ]

    # Sort chronologically by exchange timestamp
    filled_orders.sort(key=lambda x: x.get("exchange_timestamp") or x.get("order_timestamp"))

    # Filter out orders already stored in DB
    new_orders = [o for o in filled_orders if str(o["order_id"]) not in processed_order_ids]

    if not new_orders:
        return processed_symbols

    # Step 3: Sequential Order Matching Engine
    for order in new_orders:
        order_id = str(order["order_id"])
        tradingsymbol = order.get("tradingsymbol", "")
        transaction_type = order.get("transaction_type")  # "BUY" or "SELL"
        order_qty = int(order.get("filled_quantity", 0))   # Takes executed portion (e.g., 40 out of 120)
        price = float(order.get("average_price") or order.get("price") or 0.0)
        order_time = order.get("exchange_timestamp") or order.get("order_timestamp") or datetime.now()

        root_symbol = extract_root_symbol(tradingsymbol)
        option_type = "CE" if tradingsymbol.endswith("CE") else ("PE" if tradingsymbol.endswith("PE") else "EQ")
        processed_symbols.add(root_symbol)

        remaining_qty = order_qty
        opposite_type = "SELL" if transaction_type == "BUY" else "BUY"

        while remaining_qty > 0:
            open_pos = db.execute(
                text("""
                    SELECT id, quantity, entry_price, trade_type, entry_order_id, entry_time
                    FROM market_trades 
                    WHERE tradingsymbol = :tsymbol 
                      AND status = 'OPEN' 
                      AND trade_type = :opp_type
                    ORDER BY entry_time ASC 
                    LIMIT 1
                """),
                {"tsymbol": tradingsymbol, "opp_type": opposite_type}
            ).fetchone()

            if open_pos:
                pos_id, pos_qty, entry_price, trade_type, orig_entry_oid, orig_entry_time = open_pos
                entry_price = float(entry_price)
                matched_qty = min(pos_qty, remaining_qty)

                if trade_type == "BUY":
                    pnl = round((price - entry_price) * matched_qty, 2)
                else:
                    pnl = round((entry_price - price) * matched_qty, 2)

                if pos_qty > remaining_qty:
                    # PARTIAL EXIT CASE
                    db.execute(
                        text("UPDATE market_trades SET quantity = :lqty, updated_at = NOW() WHERE id = :pid"),
                        {"lqty": pos_qty - remaining_qty, "pid": pos_id}
                    )

                    db.execute(
                        text("""
                            INSERT INTO market_trades (
                                symbol, tradingsymbol, option_type, entry_order_id, exit_order_id,
                                trade_type, quantity, entry_price, exit_price, realized_pnl,
                                status, entry_time, exit_time, created_at, updated_at
                            ) VALUES (
                                :symbol, :tsymbol, :option_type, :entry_oid, :exit_oid,
                                :trade_type, :quantity, :entry_price, :exit_price, :pnl,
                                'CLOSED', :entry_time, :exit_time, NOW(), NOW()
                            )
                        """),
                        {
                            "symbol": root_symbol,
                            "tsymbol": tradingsymbol,
                            "option_type": option_type,
                            "entry_oid": orig_entry_oid,
                            "exit_oid": order_id,
                            "trade_type": trade_type,
                            "quantity": matched_qty,
                            "entry_price": entry_price,
                            "exit_price": price,
                            "pnl": pnl,
                            "entry_time": orig_entry_time,
                            "exit_time": order_time
                        }
                    )

                    logger.info(
                        f"[PARTIAL EXIT - FILLED PORTION] {tradingsymbol} | Matched: {matched_qty} | "
                        f"Entry: ₹{entry_price} -> Exit: ₹{price} | PnL: ₹{pnl}"
                    )
                    remaining_qty = 0

                else:
                    # FULL EXIT CASE
                    db.execute(
                        text("""
                            UPDATE market_trades SET
                                exit_order_id = :exit_oid,
                                exit_price = :exit_price,
                                realized_pnl = :pnl,
                                status = 'CLOSED',
                                exit_time = :exit_time,
                                updated_at = NOW()
                            WHERE id = :pos_id
                        """),
                        {
                            "exit_oid": order_id,
                            "exit_price": price,
                            "pnl": pnl,
                            "exit_time": order_time,
                            "pos_id": pos_id
                        }
                    )

                    logger.info(
                        f"[FULL EXIT - FILLED PORTION] {tradingsymbol} | Matched: {matched_qty} | "
                        f"Entry: ₹{entry_price} -> Exit: ₹{price} | PnL: ₹{pnl}"
                    )
                    remaining_qty -= matched_qty

            else:
                # NEW OPEN POSITION FOR FILLED PORTION
                db.execute(
                    text("""
                        INSERT INTO market_trades (
                            symbol, tradingsymbol, option_type, entry_order_id, trade_type,
                            quantity, entry_price, status, entry_time, created_at, updated_at
                        ) VALUES (
                            :symbol, :tsymbol, :option_type, :entry_oid, :trade_type,
                            :quantity, :entry_price, 'OPEN', :entry_time, NOW(), NOW()
                        )
                    """),
                    {
                        "symbol": root_symbol,
                        "tsymbol": tradingsymbol,
                        "option_type": option_type,
                        "entry_oid": order_id,
                        "trade_type": transaction_type,
                        "quantity": remaining_qty,
                        "entry_price": price,
                        "entry_time": order_time
                    }
                )

                logger.info(
                    f"[NEW OPEN POSITION - FILLED PORTION] {tradingsymbol} | Type: {transaction_type} | "
                    f"Qty: {remaining_qty} @ ₹{price}"
                )
                remaining_qty = 0

    db.commit()
    return processed_symbols


def trigger_summary_updates(db: Session, symbol: str = "ALL"):
    """
    Recalculates trade summaries based on the distinct dates present in market_trades
    (resolves the issue of zeroed summaries caused by date filter mismatches).
    """
    # Fetch distinct entry dates present in CLOSED market_trades
    dates_query = text("""
        SELECT DISTINCT DATE(entry_time) as trade_date 
        FROM market_trades 
        WHERE status = 'CLOSED' 
          AND (:sym = 'ALL' OR symbol = :sym)
        ORDER BY trade_date DESC 
        LIMIT 5
    """)
    active_dates = db.execute(dates_query, {"sym": symbol}).fetchall()

    # If no closed trades exist yet, default to today
    trade_dates = [d[0] for d in active_dates] if active_dates else [date.today()]

    for target_date in trade_dates:
        start_date = datetime.combine(target_date, datetime.min.time())
        end_date = datetime.combine(target_date, datetime.max.time())

        query = text("""
            SELECT 
                COUNT(*) as total_trades,
                COUNT(CASE WHEN realized_pnl > 0 THEN 1 END) as winning_trades,
                COUNT(CASE WHEN realized_pnl < 0 THEN 1 END) as losing_trades,
                COUNT(CASE WHEN option_type = 'CE' THEN 1 END) as ce_trades_count,
                COUNT(CASE WHEN option_type = 'PE' THEN 1 END) as pe_trades_count,
                COUNT(CASE WHEN option_type = 'CE' AND realized_pnl > 0 THEN 1 END) as ce_winning_count,
                COUNT(CASE WHEN option_type = 'PE' AND realized_pnl > 0 THEN 1 END) as pe_winning_count,
                COALESCE(SUM(realized_pnl), 0) as total_pnl,
                COALESCE(SUM(CASE WHEN realized_pnl > 0 THEN realized_pnl ELSE 0 END), 0) as gross_profit,
                COALESCE(SUM(CASE WHEN realized_pnl < 0 THEN ABS(realized_pnl) ELSE 0 END), 0) as gross_loss
            FROM market_trades
            WHERE status = 'CLOSED'
              AND entry_time >= :start_date 
              AND entry_time <= :end_date
              AND (:sym = 'ALL' OR symbol = :sym)
        """)

        res = db.execute(query, {
            "start_date": start_date,
            "end_date": end_date,
            "sym": symbol
        }).fetchone()

        total_trades = res.total_trades
        gross_profit = float(res.gross_profit)
        gross_loss = float(res.gross_loss)

        win_rate = round((res.winning_trades / total_trades * 100), 2) if total_trades > 0 else 0.0
        profit_factor = round((gross_profit / gross_loss), 2) if gross_loss > 0 else (
            gross_profit if gross_profit > 0 else 0.0)

        upsert_summary = text("""
            INSERT INTO market_trade_summaries (
                symbol, period_type, period_start, period_end, total_trades, winning_trades,
                losing_trades, ce_trades_count, pe_trades_count, ce_winning_count, pe_winning_count,
                total_pnl, gross_profit, gross_loss, win_rate_pct, profit_factor, created_at, updated_at
            ) VALUES (
                :sym, 'DAY', :p_start, :p_end, :total_trades, :winning_trades,
                :losing_trades, :ce_trades_count, :pe_trades_count, :ce_winning_count, :pe_winning_count,
                :total_pnl, :gross_profit, :gross_loss, :win_rate, :profit_factor, NOW(), NOW()
            )
            ON CONFLICT (symbol, period_type, period_start) DO UPDATE SET
                total_trades = EXCLUDED.total_trades,
                winning_trades = EXCLUDED.winning_trades,
                losing_trades = EXCLUDED.losing_trades,
                ce_trades_count = EXCLUDED.ce_trades_count,
                pe_trades_count = EXCLUDED.pe_trades_count,
                ce_winning_count = EXCLUDED.ce_winning_count,
                pe_winning_count = EXCLUDED.pe_winning_count,
                total_pnl = EXCLUDED.total_pnl,
                gross_profit = EXCLUDED.gross_profit,
                gross_loss = EXCLUDED.gross_loss,
                win_rate_pct = EXCLUDED.win_rate_pct,
                profit_factor = EXCLUDED.profit_factor,
                updated_at = NOW()
        """)

        db.execute(upsert_summary, {
            "sym": symbol,
            "p_start": target_date,
            "p_end": target_date,
            "total_trades": total_trades,
            "winning_trades": res.winning_trades,
            "losing_trades": res.losing_trades,
            "ce_trades_count": res.ce_trades_count,
            "pe_trades_count": res.pe_trades_count,
            "ce_winning_count": res.ce_winning_count,
            "pe_winning_count": res.pe_winning_count,
            "total_pnl": float(res.total_pnl),
            "gross_profit": gross_profit,
            "gross_loss": gross_loss,
            "win_rate": win_rate,
            "profit_factor": profit_factor
        })

    db.commit()
    logger.info(f"Summary metrics recalculated for symbol: {symbol}")