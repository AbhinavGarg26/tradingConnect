
from typing import Optional
from sqlalchemy import select
from decimal import Decimal
from trading.models import Instrument, OrderEvent, Position, Trade
from utilities.conversion.number_conversion import to_dec
from sqlalchemy.orm import Session


def get_or_create_instrument(db: Session, kite, tradingsymbol: str, exchange: str, logger) -> Optional[Instrument]:
    """Find instrument by symbol+exchange, create if missing."""

    if not (exchange in ("NSE", "BSE")):
        exchange = "NSE"

    inst = db.scalar(
        select(Instrument).where(
            Instrument.symbol   == tradingsymbol,
            Instrument.exchange == exchange,
        )
    )
    if inst:
        return inst

    logger.info("Creating new instrument: %s [%s]", tradingsymbol, exchange)

    # Try to get instrument token from Kite
    try:
        all_instruments = kite.instruments(exchange)
        match = next((i for i in all_instruments if i["tradingsymbol"] == tradingsymbol), None)
    except Exception:
        match = None

    if match:
        inst = db.scalar(
            select(Instrument).where(
                Instrument.symbol == tradingsymbol,
            )
        )
        if inst and inst.instrument_token == match["instrument_token"]:
            return inst

    segment = "EQ"
    if exchange in ("NFO", "BFO"):
        segment = "FO"
    elif exchange in ("MCX",):
        segment = "FUT"

    if not (exchange in ("NSE", "BSE")):
        exchange = "NSE"

    inst_type = "EQ"
    if match:
        inst_type = match.get("instrument_type", "EQ")
        segment   = match.get("segment", segment).split("-")[0]

    inst = Instrument(
        symbol           = tradingsymbol,
        exchange         = exchange,
        segment          = segment,
        instrument_type  = inst_type,
        instrument_token = match["instrument_token"] if match else 0,
        lot_size         = match["lot_size"] if match else 1,
        tick_size        = to_dec(match["tick_size"]) if match else Decimal("0.05"),
        expiry_date      = match.get("expiry") if match else None,
        strike_price     = to_dec(match.get("strike")) if match else None,
        is_active        = True,
    )
    db.add(inst)
    db.flush()
    return inst