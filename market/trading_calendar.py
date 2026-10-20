"""Read the exchange holiday calendar before starting live NSE work."""

from __future__ import annotations

from datetime import date

from sqlalchemy import text


def is_nse_trading_day(day: date, db=None) -> bool:
    if day.weekday() >= 5:
        return False

    if db is None:
        from trading.database import get_db
        with get_db() as session:
            return is_nse_trading_day(day, session)

    holiday = db.execute(text("""
        SELECT 1 FROM trading_holidays
        WHERE holiday_date = :day LIMIT 1
    """), {"day": day}).scalar()
    return holiday is None
