from sqlalchemy import text
from sqlalchemy.orm import Session


def get_buy_average_price(logger, db: Session, symbol: str, fallback_price: float) -> float:
    """
    Calculates the weighted buy average price strictly from 'OPEN' buy positions
    stored in the market_trades database table for a given symbol.
    """
    try:
        norm_symbol = str(symbol).strip().upper()

        # Query total monetary value (price * qty) and total quantity for OPEN buy positions
        query = text("""
            SELECT 
                SUM(entry_price * quantity) as total_value,
                SUM(quantity) as total_qty
            FROM market_trades
            WHERE UPPER(TRIM(tradingsymbol)) = :symbol
              AND status = 'OPEN'
              AND trade_type = 'BUY'
        """)

        result = db.execute(query, {"symbol": norm_symbol}).fetchone()

        if result and result.total_qty and result.total_qty > 0:
            total_value = float(result.total_value or 0)
            total_qty = float(result.total_qty)

            if total_value > 0:
                return round(total_value / total_qty, 2)

    except Exception as e:
        logger.error(f"[{symbol}] Error fetching DB buy price average: {e}")

    return float(fallback_price or 0.0)