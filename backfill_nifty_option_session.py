"""Backfill one completed NIFTY option session from Kite historical candles.

Use only after the market closes when the normal minute collector did not pin
that day's option set.  Contract selection is reconstructed from the final
NIFTY one-minute close, so the saved snapshot is explicitly a close-based
reconstruction rather than an opening-time selection.
"""
import argparse
import logging
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

from nifty_15m_env import load_nifty_environment
load_nifty_environment()

from market.option_snapshots import IST, capture_candles, select_contracts
from trading.database import get_db
from trading.user_token import fetch_user_token

log = logging.getLogger(__name__)


def completed_nifty_close(kite, session_date: date):
    opening = datetime.combine(session_date, time(9, 15), IST)
    closing = datetime.combine(session_date, time(15, 30), IST)
    rows = kite.historical_data(256265, opening, closing, "minute")
    expected = [opening + timedelta(minutes=index)
                for index in range(375)]
    stamps = [row["date"].astimezone(IST) if row["date"].tzinfo else row["date"].replace(tzinfo=IST)
              for row in rows]
    if stamps != expected:
        raise RuntimeError("NIFTY session is incomplete; refusing option backfill")
    close = float(rows[-1]["close"])
    if close <= 0:
        raise RuntimeError("NIFTY close is invalid")
    return close, closing


def save_selection(db, session_date, snapshot_at, spot, contracts):
    for role, atm, contract in contracts:
        db.execute(text("""INSERT INTO nifty_option_snapshots
            (session_date, expiry_role, snapshot_at, nifty_spot, atm_strike,
             instrument_token, symbol, expiry, strike, option_type, lot_size, tick_size)
            VALUES (:day,:role,:at,:spot,:atm,:token,:symbol,:expiry,:strike,:kind,:lot,:tick)
            ON CONFLICT (session_date, expiry_role, instrument_token) DO NOTHING"""), {
                "day": session_date, "role": role, "at": snapshot_at, "spot": spot,
                "atm": atm, "token": contract["instrument_token"], "symbol": contract["symbol"],
                "expiry": contract["expiry_date"], "strike": contract["strike_price"],
                "kind": contract["instrument_type"], "lot": contract["lot_size"],
                "tick": contract["tick_size"],
            })
    db.commit()


def backfill(kite, session_date: date):
    spot, closing = completed_nifty_close(kite, session_date)
    master = kite.instruments("NFO")
    contracts = select_contracts(master, spot, session_date)
    with get_db() as db:
        save_selection(db, session_date, closing, spot, contracts)
        capture_candles(kite, db, closing)
        saved = db.execute(text("SELECT COUNT(*) FROM nifty_option_candles WHERE session_date=:day"),
                           {"day": session_date}).scalar_one()
    return {"date": str(session_date), "spot_close": spot, "contracts": len(contracts), "candles": saved}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat, default=datetime.now(IST).date())
    parser.add_argument("--user-id", type=int, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    kite, user_id = fetch_user_token(log, user_id=args.user_id)
    if user_id != args.user_id:
        raise RuntimeError("Authenticated account does not match requested user")
    print(backfill(kite, args.date))


if __name__ == "__main__":
    main()
