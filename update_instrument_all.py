"""
update_lot_sizes.py
───────────────────
Updates lot_size (and optionally tick_size) for ALL instruments in the DB
by matching against resources/instrument_list.csv.

Unlike instrument_sync.py which only touches open-trade instruments,
this script runs across the entire instruments table.

Run:
    python update_lot_sizes.py
    python update_lot_sizes.py --dry-run
    python update_lot_sizes.py --exchange NFO
    python update_lot_sizes.py --exchange NFO --dry-run
"""

import argparse
import logging
import sys

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy.orm import Session
from sqlalchemy import text

load_dotenv()

from trading.user_token import fetch_user_token
from trading.database import get_db

# ─────────────────────────── CONFIG ────────────────────────────────────────
INSTRUMENTS_CSV = "resources/instrument_list.csv"
# ───────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("update_lot_sizes")


# ── 1. Load all instruments from DB ───────────────────────────────────────────

def fetch_all_instruments(db: Session, exchange: str = None) -> pd.DataFrame:
    """
    Fetch all instruments from DB.
    Optionally filter by exchange (e.g. NFO, NSE, BSE, BFO).
    """
    where = "WHERE inst.exchange = :exchange" if exchange else ""
    params = {"exchange": exchange} if exchange else {}

    sql = text(f"""
        SELECT
            inst.id               AS instrument_id,
            inst.symbol           AS symbol,
            inst.exchange         AS exchange,
            inst.instrument_type  AS instrument_type,
            inst.lot_size         AS lot_size,
            inst.tick_size        AS tick_size
        FROM instruments inst
        {where}
        ORDER BY inst.exchange, inst.symbol
    """)

    df = pd.read_sql(sql, db.bind, params=params)

    scope = f"exchange={exchange}" if exchange else "all exchanges"
    logger.info("Fetched %d instruments from DB (%s)", len(df), scope)
    return df


# ── 2. Load CSV ────────────────────────────────────────────────────────────────

def load_csv_instruments() -> pd.DataFrame:
    """
    Load instrument master from CSV and normalise types.
    Builds a lookup keyed by (tradingsymbol, exchange) for O(1) matching.
    """
    logger.info("Reading %s …", INSTRUMENTS_CSV)
    df = pd.read_csv(INSTRUMENTS_CSV, low_memory=False)

    df["lot_size"]  = pd.to_numeric(df["lot_size"],  errors="coerce").astype("Int64")
    df["tick_size"] = pd.to_numeric(df["tick_size"], errors="coerce")

    # Drop rows where both are null — nothing to update from them
    df = df.dropna(subset=["lot_size", "tick_size"], how="all")

    logger.info("Loaded %d usable rows from CSV", len(df))
    return df


def build_csv_lookup(csv_df: pd.DataFrame) -> dict:
    """
    Returns dict keyed by (tradingsymbol, exchange) → Series row.
    When the same symbol appears in multiple rows (e.g. multiple expiries
    for a future), all rows for that symbol share the same lot_size/tick_size
    so any row is fine — we take the first.
    """
    lookup = {}
    for _, row in csv_df.iterrows():
        key = (row["tradingsymbol"], row["exchange"], row["instrument_type"])
        if key not in lookup:
            lookup[key] = row
    logger.info("CSV lookup built: %d unique (symbol, exchange) keys", len(lookup))
    return lookup


# ── 3. Underlying parser for expired contracts ────────────────────────────────

import re

# Patterns ordered most-specific first
# BSE Sensex weekly: SENSEX2651474800CE  → SENSEX
# NSE/BSE index weekly: NIFTY2551422200CE → NIFTY
# Monthly stock option: ABB26MAR6150PE   → ABB
# Monthly index option: BANKNIFTY26MAR52000CE → BANKNIFTY
# Futures: NIFTY25MAYFUT / ABB26MARFUT   → NIFTY / ABB
_EXPIRY_PATTERNS = [
    # weekly  DDMMMYYYY or DDMMYY digits-only expiry (BSE style): SENSEX2651474800CE
    re.compile(r"^([A-Z&-]+?)\d{5,}\d+(?:\.\d+)?(CE|PE|FUT)$"),
    # monthly text expiry: ABB26MAR6150PE / BANKNIFTY26MAR52000CE
    re.compile(r"^([A-Z&-]+?)\d{2}[A-Z]{3}\d+(?:\.\d+)?(CE|PE|FUT)$"),
    # futures monthly text: NIFTY25MAYFUT
    re.compile(r"^([A-Z&-]+?)\d{2}[A-Z]{3}FUT$"),
    # weekly numeric expiry futures: NIFTY2551WFUT (edge case)
    re.compile(r"^([A-Z&-]+?)\d+(FUT)$"),
]

def extract_underlying(symbol: str) -> str | None:
    """
    Parse the underlying name from an expired contract symbol.
    Examples:
      SENSEX2651474800CE → SENSEX
      ABB26MAR6150PE     → ABB
      NIFTY25MAYFUT      → NIFTY
      ANGELONE26MAR225CE → ANGELONE
    """
    for pat in _EXPIRY_PATTERNS:
        m = pat.match(symbol)
        if m:
            return m.group(1)
    return None


def build_underlying_lookup(csv_df: pd.DataFrame) -> dict:
    """
    Secondary lookup keyed by (underlying_name, exchange) → lot_size, tick_size.
    Built from the CSV by stripping expiry/strike from each tradingsymbol.
    Covers cases where the exact expired contract isn't in the CSV.
    """
    lookup = {}
    for _, row in csv_df.iterrows():
        underlying = extract_underlying(row["tradingsymbol"])
        if underlying is None:
            # plain equity like RELIANCE, INFY — use symbol itself
            underlying = row["name"]
        key = (underlying, row["exchange"], row['instrument_type'])
        # prefer rows with lot_size populated; first-seen wins
        if key not in lookup and pd.notna(row.get("lot_size")):
            lookup[key] = row
    logger.info(
        "Underlying lookup built: %d unique (underlying, exchange) keys", len(lookup)
    )
    return lookup


# ── 4. Compute diff ────────────────────────────────────────────────────────────

def build_updates(db_df: pd.DataFrame, csv_lookup: dict, underlying_lookup: dict) -> list[dict]:
    """
    For each DB instrument:
      1. Try exact (symbol, exchange) match in csv_lookup
      2. If not found (expired contract), parse underlying and try underlying_lookup
      3. Build update only if lot_size or tick_size has changed
    """
    updates          = []
    resolved_exact   = 0
    resolved_expired = 0
    unresolvable     = []

    for _, row in db_df.iterrows():
        symbol   = row["symbol"]
        exchange = row["exchange"]
        instrument_type = row["instrument_type"]

        # ── exact match ──────────────────────────────────────────────────
        csv_row = csv_lookup.get((symbol, exchange, instrument_type))
        if csv_row is not None:
            resolved_exact += 1
            source = "exact"
        else:
            # ── fallback: underlying match for expired contracts ─────────
            underlying = extract_underlying(symbol) or symbol
            csv_row    = underlying_lookup.get((underlying, exchange, instrument_type))
            if csv_row is None and instrument_type in ["CE", "PE", "FUT"]:
                csv_row = underlying_lookup.get((underlying, "BFO", instrument_type))
            if csv_row is None and instrument_type in ["CE", "PE", "FUT"]:
                csv_row = underlying_lookup.get((underlying, "NFO", instrument_type))
            if csv_row is not None:
                resolved_expired += 1
                source = f"underlying({underlying})"
            else:
                unresolvable.append(f"{exchange}:{symbol}")
                continue

        new_lot  = int(csv_row["lot_size"])    if pd.notna(csv_row.get("lot_size"))  else None
        new_tick = float(csv_row["tick_size"]) if pd.notna(csv_row.get("tick_size")) else None

        old_lot  = int(row["lot_size"])    if pd.notna(row["lot_size"])  else None
        old_tick = float(row["tick_size"]) if pd.notna(row["tick_size"]) else None

        lot_changed  = new_lot  != old_lot
        tick_changed = new_tick != old_tick

        if not lot_changed and not tick_changed:
            continue

        changes = []
        if lot_changed:  changes.append(f"lot_size  {old_lot} → {new_lot}")
        if tick_changed: changes.append(f"tick_size {old_tick} → {new_tick}")

        logger.info(
            "  %-40s [%s]  %-20s  %s",
            symbol, exchange, f"via {source}", " | ".join(changes)
        )

        updates.append({
            "instrument_id": int(row["instrument_id"]),
            "new_lot_size":  new_lot,
            "new_tick_size": new_tick,
        })

    logger.info(
        "Resolved — exact: %d  |  expired via underlying: %d  |  unresolvable: %d",
        resolved_exact, resolved_expired, len(unresolvable)
    )

    if unresolvable:
        logger.warning(
            "%d instruments could not be resolved (no underlying match):\n  %s",
            len(unresolvable), "\n  ".join(unresolvable[:30])
        )
        if len(unresolvable) > 30:
            logger.warning("  … and %d more", len(unresolvable) - 30)

    return updates


# ── 4. Apply updates ───────────────────────────────────────────────────────────

def apply_updates(db: Session, updates: list[dict], dry_run: bool):
    if not updates:
        logger.info("All instruments already up to date — nothing to write")
        return

    if dry_run:
        logger.info("[DRY RUN] Would update lot_size/tick_size for %d instruments — skipping DB write", len(updates))
        return

    sql = text("""
        UPDATE instruments
        SET lot_size   = :new_lot_size,
            tick_size  = :new_tick_size,
            updated_at = NOW()
        WHERE id = :instrument_id
    """)

    db.execute(sql, updates)
    logger.info("Updated lot_size + tick_size for %d instruments", len(updates))


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Bulk update lot_size + tick_size for all instruments")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would change without writing to DB"
    )
    parser.add_argument(
        "--exchange", default=None,
        help="Restrict to a single exchange (e.g. NFO, NSE, BSE, BFO)"
    )
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("Lot Size Update  |  exchange=%s  |  dry_run=%s",
                args.exchange or "ALL", args.dry_run)
    logger.info("=" * 60)

    # Kite token — kept consistent with kite_sync.py pattern
    # Not strictly needed here since we only use CSV, but keeps
    # auth bootstrapping uniform across all sync scripts
    kite, user_id = fetch_user_token(logger)

    with get_db() as db:

        logger.info("\n[1/3] Fetching instruments from DB …")
        db_df = fetch_all_instruments(db, exchange=args.exchange)

        if db_df.empty:
            logger.info("No instruments found in DB — nothing to do")
            return

        logger.info("\n[2/3] Loading CSV master …")
        csv_df      = load_csv_instruments()
        csv_lookup  = build_csv_lookup(csv_df)
        underlying_lookup = build_underlying_lookup(csv_df)

        logger.info("\n[3/3] Computing diff and applying updates …")
        updates = build_updates(db_df, csv_lookup, underlying_lookup)
        apply_updates(db, updates, dry_run=args.dry_run)

    logger.info("\n" + "=" * 60)
    logger.info(
        "Done%s  |  %d instruments updated",
        " (DRY RUN)" if args.dry_run else "",
        len(updates) if not args.dry_run else 0
    )
    logger.info("=" * 60)


if __name__ == "__main__":
    main()