"""Run Zerodha Trade Journal reconciliation once, including after market close.

Usage on the application server:
    .venv/bin/python reconcile_trade_journal.py --user-id 975447485

The broker session must still be valid. The script uses the existing project
environment and writes only through the normal idempotent reconciliation path.
"""

from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


logger = logging.getLogger("trade_journal_reconciliation")
IST = ZoneInfo("Asia/Kolkata")


def load_nifty_environment() -> None:
    """Load database and encryption settings before importing trading modules."""
    from nifty_15m_env import load_nifty_environment as load_project_environment

    load_project_environment()
    if not os.getenv("DB_ENCRYPTION_KEY"):
        raise RuntimeError(
            "DB_ENCRYPTION_KEY is missing. Set it in the application's .env "
            "or service environment before running reconciliation."
        )


def journal_totals(db, user_id: int, exchange_link_id: str, day_start: datetime) -> tuple[float, float]:
    """Return today's realised journal gross P&L and recorded charges."""
    from sqlalchemy import text

    row = db.execute(text("""
        SELECT COALESCE(SUM(realized_pnl), 0) AS gross,
               COALESCE(SUM(total_charges), 0) AS charges
        FROM market_trades
        WHERE user_id = :user_id
          AND exchange_link_id = CAST(:exchange_link_id AS UUID)
          AND status = 'CLOSED'
          AND exit_time >= :day_start
          AND exit_time < :day_end
    """), {
        "user_id": user_id,
        "exchange_link_id": exchange_link_id,
        "day_start": day_start,
        "day_end": day_start + timedelta(days=1),
    }).one()
    return round(float(row.gross), 2), round(float(row.charges), 2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", type=int, required=True, help="Owner of the Zerodha connection")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    load_nifty_environment()
    # Import after loading .env: trading.exchange_link checks its encryption
    # key at module import time.
    from analytics.kite_sync_orders import trigger_summary_updates
    from analytics.trade_reconciliation import reconcile_trades_from_start_of_day, resolve_trade_attribution
    from trading.database import get_db
    from trading.user_token import fetch_user_token

    kite, user_id = fetch_user_token(logger, user_id=args.user_id)
    day_start = datetime.combine(datetime.now(IST).date(), time.min)
    with get_db() as db:
        attribution = resolve_trade_attribution(db, user_id)
        if attribution is None:
            raise RuntimeError("Expected exactly one active Zerodha connection for this user")
        exchange_link_id = attribution[1]
        before_gross, before_charges = journal_totals(db, user_id, exchange_link_id, day_start)
        symbols = reconcile_trades_from_start_of_day(kite, db, user_id)
        # The first pass can repair detailed executions after comparing the
        # broker totals. A second idempotent pass settles any adjustment
        # against those newly repaired rows before summaries are refreshed.
        symbols |= reconcile_trades_from_start_of_day(kite, db, user_id)
        for symbol in sorted(symbols):
            trigger_summary_updates(db, user_id=user_id, symbol=symbol)
        if symbols:
            trigger_summary_updates(db, user_id=user_id, symbol="ALL")
        after_gross, after_charges = journal_totals(db, user_id, exchange_link_id, day_start)

    print(f"Reconciled symbols: {', '.join(sorted(symbols)) or 'none'}")
    print(f"Journal gross P&L: ₹{before_gross:,.2f} → ₹{after_gross:,.2f}")
    print(f"Recorded charges: ₹{before_charges:,.2f} → ₹{after_charges:,.2f}")
    print(f"Journal net P&L: ₹{before_gross - before_charges:,.2f} → ₹{after_gross - after_charges:,.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
