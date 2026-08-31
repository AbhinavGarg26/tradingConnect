import logging
import re
from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session


logger = logging.getLogger("MarketAnalytics")


def extract_root_symbol(tradingsymbol: str) -> str:
    match = re.match(r"^([A-Z\-]+)", tradingsymbol)
    return match.group(1) if match else tradingsymbol


def parse_option_type(tradingsymbol: str) -> str:
    if tradingsymbol.endswith("CE"):
        return "CE"
    if tradingsymbol.endswith("PE"):
        return "PE"
    if tradingsymbol.endswith("FUT"):
        return "FUT"
    return "EQ"


def process_and_merge_trades(kite, db: Session) -> set:
    """Compatibility wrapper for execution-level start-of-day reconciliation."""
    from analytics.trade_reconciliation import reconcile_trades_from_start_of_day
    return reconcile_trades_from_start_of_day(kite, db)


def trigger_summary_updates(db: Session, symbol: str = "ALL"):
    """Recalculate daily summaries for recent dates containing closed trades."""
    dates_query = text("""
        SELECT DISTINCT DATE(entry_time) AS trade_date
        FROM market_trades
        WHERE status = 'CLOSED'
          AND (:sym = 'ALL' OR symbol = :sym)
        ORDER BY trade_date DESC
        LIMIT 5
    """)
    active_dates = db.execute(dates_query, {"sym": symbol}).fetchall()
    trade_dates = [row[0] for row in active_dates] if active_dates else [date.today()]

    for target_date in trade_dates:
        start_date = datetime.combine(target_date, datetime.min.time())
        end_date = datetime.combine(target_date, datetime.max.time())
        result = db.execute(text("""
            SELECT
                COUNT(*) AS total_trades,
                COUNT(CASE WHEN realized_pnl > 0 THEN 1 END) AS winning_trades,
                COUNT(CASE WHEN realized_pnl < 0 THEN 1 END) AS losing_trades,
                COUNT(CASE WHEN option_type = 'CE' THEN 1 END) AS ce_trades_count,
                COUNT(CASE WHEN option_type = 'PE' THEN 1 END) AS pe_trades_count,
                COUNT(CASE WHEN option_type = 'CE' AND realized_pnl > 0 THEN 1 END) AS ce_winning_count,
                COUNT(CASE WHEN option_type = 'PE' AND realized_pnl > 0 THEN 1 END) AS pe_winning_count,
                COALESCE(SUM(realized_pnl), 0) AS total_pnl,
                COALESCE(SUM(CASE WHEN realized_pnl > 0 THEN realized_pnl ELSE 0 END), 0) AS gross_profit,
                COALESCE(SUM(CASE WHEN realized_pnl < 0 THEN ABS(realized_pnl) ELSE 0 END), 0) AS gross_loss
            FROM market_trades
            WHERE status = 'CLOSED'
              AND entry_time >= :start_date
              AND entry_time <= :end_date
              AND (:sym = 'ALL' OR symbol = :sym)
        """), {
            "start_date": start_date,
            "end_date": end_date,
            "sym": symbol,
        }).fetchone()

        total_trades = int(result.total_trades or 0)
        gross_profit = float(result.gross_profit or 0)
        gross_loss = float(result.gross_loss or 0)
        win_rate = round((result.winning_trades / total_trades) * 100, 2) if total_trades else 0.0
        profit_factor = (
            round(gross_profit / gross_loss, 2)
            if gross_loss > 0
            else (gross_profit if gross_profit > 0 else 0.0)
        )

        db.execute(text("""
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
        """), {
            "sym": symbol,
            "p_start": target_date,
            "p_end": target_date,
            "total_trades": total_trades,
            "winning_trades": result.winning_trades,
            "losing_trades": result.losing_trades,
            "ce_trades_count": result.ce_trades_count,
            "pe_trades_count": result.pe_trades_count,
            "ce_winning_count": result.ce_winning_count,
            "pe_winning_count": result.pe_winning_count,
            "total_pnl": float(result.total_pnl or 0),
            "gross_profit": gross_profit,
            "gross_loss": gross_loss,
            "win_rate": win_rate,
            "profit_factor": profit_factor,
        })

    db.commit()
    logger.info("Summary metrics recalculated for symbol: %s", symbol)
