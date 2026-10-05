"""Read broker-position prices from the authenticated Kite market-data API."""

from __future__ import annotations


def quote_key(exchange: str | None, segment: str | None, symbol: str) -> str:
    """Translate a Groww exchange/segment pair to Kite's quote key."""
    source_exchange = str(exchange or "NSE").upper()
    source_segment = str(segment or "CASH").upper()
    if source_segment == "FNO":
        source_exchange = {"NSE": "NFO", "BSE": "BFO"}.get(source_exchange, source_exchange)
    return f"{source_exchange}:{symbol}"


def live_prices(kite, positions: list[dict]) -> dict[tuple[str, str, str], float]:
    """Fetch all requested LTPs in one Kite call, keyed by broker position."""
    keys: dict[tuple[str, str, str], str] = {}
    for position in positions:
        symbol = str(position.get("trading_symbol") or position.get("symbol") or "")
        if not symbol:
            continue
        exchange = str(position.get("exchange") or "NSE").upper()
        segment = str(position.get("segment") or "CASH").upper()
        keys[(exchange, segment, symbol)] = quote_key(exchange, segment, symbol)

    response = kite.ltp(list(keys.values())) if keys else {}
    prices: dict[tuple[str, str, str], float] = {}
    for position_key, key in keys.items():
        price = float((response.get(key) or {}).get("last_price") or 0)
        if price > 0:
            prices[position_key] = price
    return prices
