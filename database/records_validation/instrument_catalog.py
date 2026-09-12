"""Mapping helpers for authoritative Kite instrument-master records."""

from datetime import date, datetime


def catalog_values(kite_row: dict) -> dict:
    expiry = kite_row.get("expiry")
    if isinstance(expiry, datetime):
        expiry = expiry.date()
    elif isinstance(expiry, str) and expiry:
        expiry = date.fromisoformat(expiry[:10])
    elif not expiry:
        expiry = None

    strike = float(kite_row.get("strike") or 0)
    return {
        "symbol": str(kite_row["tradingsymbol"]).upper(),
        "exchange": str(kite_row["exchange"]).upper(),
        "segment": str(kite_row.get("segment") or kite_row["exchange"]).upper(),
        "instrument_type": str(kite_row.get("instrument_type") or "EQ").upper(),
        "instrument_token": int(kite_row["instrument_token"]),
        "lot_size": int(kite_row.get("lot_size") or 1),
        "tick_size": float(kite_row.get("tick_size") or 0.05),
        "expiry_date": expiry,
        "strike_price": strike if strike else None,
    }
