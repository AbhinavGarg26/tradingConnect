"""Official NSE index universes and idempotent equity instrument synchronisation."""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from sqlalchemy import text


INDEX_URLS = {
    "nifty500": "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv",
    "midcap150": "https://www.niftyindices.com/IndexConstituent/ind_niftymidcap150list.csv",
    "smallcap500": "https://www.niftyindices.com/IndexConstituent/ind_niftysmallcap500list.csv",
}
DEFAULT_INDEXES = ("nifty500", "midcap150", "smallcap500")
DEFAULT_CACHE_DIR = Path(__file__).resolve().parents[1] / "resources" / "index_constituents"


def _symbols(payload: str) -> list[str]:
    rows = csv.DictReader(io.StringIO(payload.lstrip("\ufeff")))
    if not rows.fieldnames:
        raise ValueError("Index constituent CSV has no header")
    symbol_key = next((name for name in rows.fieldnames if name.strip().lower() == "symbol"), None)
    if symbol_key is None:
        raise ValueError("Index constituent CSV has no Symbol column")
    return sorted({str(row.get(symbol_key, "")).strip().upper() for row in rows
                   if str(row.get(symbol_key, "")).strip()})


def download_constituents(index: str, *, cache_dir: Path = DEFAULT_CACHE_DIR,
                          refresh: bool = False, timeout: int = 30) -> list[str]:
    """Read a cached official constituent file, downloading it when necessary."""
    if index not in INDEX_URLS:
        raise ValueError(f"Unknown index {index!r}; choose from {sorted(INDEX_URLS)}")
    path = Path(cache_dir) / f"{index}.csv"
    if refresh or not path.is_file():
        request = Request(INDEX_URLS[index], headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8-sig")
        values = _symbols(payload)
        if not values:
            raise ValueError(f"Official {index} constituent file is empty")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
        return values
    return _symbols(path.read_text(encoding="utf-8"))


def load_equity_universe(indexes=DEFAULT_INDEXES, *, cache_dir: Path = DEFAULT_CACHE_DIR,
                         refresh: bool = False) -> tuple[str, ...]:
    """Return the sorted, deduplicated union of the selected current constituents."""
    values = set()
    for index in indexes:
        values.update(download_constituents(index, cache_dir=cache_dir, refresh=refresh))
    return tuple(sorted(values))


def kite_equity_catalog(kite) -> dict[str, dict]:
    """Return current NSE EQ instruments keyed by exact trading symbol."""
    catalog = {}
    for row in kite.instruments("NSE"):
        symbol = str(row.get("tradingsymbol", "")).strip().upper()
        if symbol and str(row.get("instrument_type", "")).upper() == "EQ":
            catalog[symbol] = row
    return catalog


def sync_equity_instruments(db, kite, symbols) -> dict:
    """Upsert current constituent equities into the shared instruments table."""
    catalog = kite_equity_catalog(kite)
    matched = []
    missing = []
    now = datetime.now(timezone.utc)
    for symbol in symbols:
        row = catalog.get(symbol)
        if row is None:
            missing.append(symbol)
            continue
        matched.append({
            "symbol": symbol,
            "exchange": "NSE",
            "segment": str(row.get("segment") or "NSE").upper(),
            "instrument_type": "EQ",
            "instrument_token": int(row["instrument_token"]),
            "lot_size": int(row.get("lot_size") or 1),
            "tick_size": float(row.get("tick_size") or .05),
            "expiry_date": row.get("expiry") or None,
            "strike_price": float(row.get("strike") or 0),
            "now": now,
        })
    if matched:
        db.execute(text("""
            INSERT INTO instruments (
                symbol, exchange, segment, instrument_type, instrument_token,
                lot_size, tick_size, expiry_date, strike_price, is_active,
                created_at, updated_at
            ) VALUES (
                :symbol, :exchange, :segment, :instrument_type, :instrument_token,
                :lot_size, :tick_size, :expiry_date, :strike_price, TRUE,
                :now, :now
            )
            ON CONFLICT (instrument_token) DO UPDATE SET
                symbol = EXCLUDED.symbol, exchange = EXCLUDED.exchange,
                segment = EXCLUDED.segment, instrument_type = EXCLUDED.instrument_type,
                lot_size = EXCLUDED.lot_size, tick_size = EXCLUDED.tick_size,
                expiry_date = EXCLUDED.expiry_date, strike_price = EXCLUDED.strike_price,
                is_active = TRUE, updated_at = EXCLUDED.updated_at
        """), matched)
        db.commit()
    return {"requested": len(symbols), "upserted": len(matched), "missing": missing,
            "as_of": str(date.today())}
