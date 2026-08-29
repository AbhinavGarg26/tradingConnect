import math
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import text


def log_trade(db: Session, trade_data: dict, snapshot_id: int = None):
    """Inserts or updates an individual trade execution."""
    trade_data["market_snapshot_id"] = snapshot_id
    trade_data["created_at"] = datetime.now()
    trade_data["updated_at"] = datetime.now()

    cols = ", ".join(trade_data.keys())
    ph = ", ".join(f":{k}" for k in trade_data.keys())
    db.execute(text(f"INSERT INTO trades ({cols}) VALUES ({ph})"), trade_data)
    db.commit()


def generate_and_save_summary(db: Session, symbol: str, period_type: str, start_date: str, end_date: str):
    """
    Aggregates trades from the `trades` table for a specific symbol (or "ALL")
    over a DAY, WEEK, or MONTH window and upserts into `trade_summaries`.
    """
    symbol_filter = "" if symbol == "ALL" else "AND symbol = :symbol"

    query = text(f"""
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
        FROM trades
        WHERE status = 'CLOSED'
          AND entry_time >= :start_date 
          AND entry_time <= :end_date
          {symbol_filter}
    """)

    params = {"start_date": start_date, "end_date": end_date}
    if symbol != "ALL":
        params["symbol"] = symbol

    res = db.execute(query, params).fetchone()

    total_trades = res.total_trades
    gross_profit = float(res.gross_profit)
    gross_loss = float(res.gross_loss)

    win_rate = round((res.winning_trades / total_trades * 100), 2) if total_trades > 0 else 0.0
    profit_factor = round((gross_profit / gross_loss), 2) if gross_loss > 0 else (
        gross_profit if gross_profit > 0 else 0.0)

    summary_payload = {
        "symbol": symbol,
        "period_type": period_type,
        "period_start": start_date,
        "period_end": end_date,
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
        "win_rate_pct": win_rate,
        "profit_factor": profit_factor,
        "updated_at": datetime.now()
    }

    # Upsert logic into trade_summaries
    upsert_sql = text("""
        INSERT INTO trade_summaries (
            symbol, period_type, period_start, period_end, total_trades, winning_trades, 
            losing_trades, ce_trades_count, pe_trades_count, ce_winning_count, pe_winning_count, 
            total_pnl, gross_profit, gross_loss, win_rate_pct, profit_factor, created_at, updated_at
        ) VALUES (
            :symbol, :period_type, :period_start, :period_end, :total_trades, :winning_trades, 
            :losing_trades, :ce_trades_count, :pe_trades_count, :ce_winning_count, :pe_winning_count, 
            :total_pnl, :gross_profit, :gross_loss, :win_rate_pct, :profit_factor, NOW(), :updated_at
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
            updated_at = EXCLUDED.updated_at
    """)

    db.execute(upsert_sql, summary_payload)
    db.commit()