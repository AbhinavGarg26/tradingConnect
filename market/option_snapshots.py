"""Durable daily NIFTY option selection and completed minute candles."""
import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

from backtest.session_calendar import regular_session_closed
from database.records_validation.instrument_catalog import catalog_values

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger(__name__)


def select_contracts(master, spot, today):
    """Select two nearest expiries plus the first upcoming monthly expiry."""
    rows = [catalog_values(row) for row in master
            if row.get("name") == "NIFTY" and row.get("exchange") == "NFO"
            and row.get("instrument_type") in ("CE", "PE")]
    rows = [row for row in rows if row["expiry_date"] and row["expiry_date"] >= today
            and row["strike_price"] is not None]
    expiries = sorted({row["expiry_date"] for row in rows})
    if len(expiries) < 2:
        raise ValueError("NIFTY weekly expiries are missing from NFO master")
    month_ends = { (expiry.year, expiry.month): expiry for expiry in expiries }
    monthly = month_ends[min(month_ends)]
    roles = {"nearest_weekly": expiries[0], "next_weekly": expiries[1],
             "monthly": monthly}
    selection = []
    for role, expiry in roles.items():
        chain = [row for row in rows if row["expiry_date"] == expiry]
        strikes = sorted({row["strike_price"] for row in chain
                          if {x["instrument_type"] for x in chain
                              if x["strike_price"] == row["strike_price"]} == {"CE", "PE"}})
        if len(strikes) < 5:
            raise ValueError(f"Incomplete NIFTY option chain for {expiry}")
        atm = min(strikes, key=lambda strike: (abs(strike - spot), strike))
        index = strikes.index(atm)
        if index < 2 or index + 2 >= len(strikes):
            raise ValueError(f"ATM ±2 strikes unavailable for {expiry}")
        selected = set(strikes[index - 2:index + 3])
        chosen = [row for row in chain if row["strike_price"] in selected]
        if len(chosen) != 10:
            raise ValueError(f"Duplicate or missing option tokens for {expiry}")
        selection.extend((role, atm, row) for row in chosen)
    return selection


def capture_snapshot(kite, db, now, master):
    day = now.date()
    if db.execute(text("SELECT 1 FROM nifty_option_snapshots WHERE session_date=:day LIMIT 1"),
                  {"day": day}).first():
        return
    quote = kite.quote(["NSE:NIFTY 50"])["NSE:NIFTY 50"]
    stamp = quote.get("timestamp")
    if stamp:
        if isinstance(stamp, str):
            stamp = datetime.fromisoformat(stamp)
        stamp = stamp.replace(tzinfo=IST) if stamp.tzinfo is None else stamp.astimezone(IST)
        if stamp.date() != day or now - stamp > timedelta(minutes=3):
            raise ValueError("NIFTY spot quote is stale")
    spot = float(quote["last_price"])
    if spot <= 0:
        raise ValueError("NIFTY spot is invalid")
    selected = select_contracts(master, spot, day)
    for role, atm, row in selected:
        db.execute(text("""INSERT INTO nifty_option_snapshots
            (session_date, expiry_role, snapshot_at, nifty_spot, atm_strike,
             instrument_token, symbol, expiry, strike, option_type, lot_size, tick_size)
            VALUES (:day,:role,:now,:spot,:atm,:token,:symbol,:expiry,:strike,:type,:lot,:tick)
            ON CONFLICT (session_date, expiry_role, instrument_token) DO NOTHING"""),
            {"day": day, "role": role, "now": now, "spot": spot, "atm": atm,
             "token": row["instrument_token"], "symbol": row["symbol"],
             "expiry": row["expiry_date"], "strike": row["strike_price"],
             "type": row["instrument_type"], "lot": row["lot_size"],
             "tick": row["tick_size"]})
    db.commit()
    log.info("Saved %d NIFTY option selections for %s", len(selected), day)


def capture_candles(kite, db, now):
    opening = now.replace(hour=9, minute=15, second=0, microsecond=0)
    end = min(now.replace(second=0, microsecond=0), opening.replace(hour=15, minute=30))
    if end <= opening:
        return
    tokens = [row[0] for row in db.execute(text("""SELECT DISTINCT instrument_token
        FROM nifty_option_snapshots WHERE session_date=:day ORDER BY instrument_token"""),
        {"day": now.date()})]
    for token in tokens:
        # Begin at the earliest missing minute, including gaps in a partial API response.
        start = db.execute(text("""SELECT min(expected.candle_at)
            FROM generate_series(:opening, :last, interval '1 minute') AS expected(candle_at)
            WHERE NOT EXISTS (SELECT 1 FROM nifty_option_candles saved
                WHERE saved.instrument_token=:token AND saved.candle_at=expected.candle_at)"""),
            {"opening": opening, "last": end - timedelta(minutes=1), "token": token}).scalar()
        if start is None:
            continue
        start = start.replace(tzinfo=IST) if start.tzinfo is None else start.astimezone(IST)
        if start >= end:
            continue
        try:
            candles = kite.historical_data(token, start, end, "minute")
            for candle in candles:
                stamp = candle["date"]
                if isinstance(stamp, str):
                    stamp = datetime.fromisoformat(stamp)
                stamp = stamp.replace(tzinfo=IST) if stamp.tzinfo is None else stamp.astimezone(IST)
                if not opening <= stamp < end:
                    continue
                prices = {key: float(candle[key]) for key in ("open", "high", "low", "close")}
                if min(prices.values()) <= 0 or prices["low"] > min(prices["open"], prices["close"]) or prices["high"] < max(prices["open"], prices["close"]):
                    raise ValueError(f"Invalid candle for token {token} at {stamp}")
                db.execute(text("""INSERT INTO nifty_option_candles
                    (session_date,instrument_token,candle_at,open,high,low,close,volume,open_interest)
                    VALUES (:day,:token,:stamp,:open,:high,:low,:close,:volume,:oi)
                    ON CONFLICT (instrument_token,candle_at) DO NOTHING"""),
                    {"day": now.date(), "token": token, "stamp": stamp, **prices,
                     "volume": candle.get("volume"), "oi": candle.get("oi")})
            db.commit()
        except Exception:
            db.rollback()
            log.exception("Option minute capture failed for token %s", token)
        time.sleep(0.4)  # Respect Kite's historical-data request rate.


def run(kite, db, now, master):
    if regular_session_closed(now.date()) or not (9, 16) <= (now.hour, now.minute) <= (15, 35):
        return
    capture_snapshot(kite, db, now, master)
    capture_candles(kite, db, now)
