"""
instrument_sync.py
──────────────────
1. Fetch open-trade instruments (mirrors the Ruby InvestmentUnMatch query)
2. Resolve correct strike_price_price + instrument_token from resources/instrument_list.csv
3. Update instruments table with corrected values
4. Mark instruments with expiry_date_date < today as inactive
5. Fetch live LTP from Kite for FUTURES and EQUITY instruments and update current_price

Run:
    python instrument_sync.py [--dry-run]
"""

import argparse
import logging
import sys
from datetime import date
from typing import Optional

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy.orm import Session
from sqlalchemy import text

load_dotenv()

from trading.user_token import fetch_user_token
from trading.database import get_db

# ─────────────────────────── CONFIG ────────────────────────────────────────
INSTRUMENTS_CSV          = "resources/instrument_list.csv"
MATCH_STATUS_ALL_PENDING = ("pending","partial")   # adjust to match your Ruby constant values
# ───────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("instrument_sync")


# ── 1. Fetch open trades ───────────────────────────────────────────────────────

def fetch_open_trade_instruments(db: Session) -> pd.DataFrame:
    """
    Mirrors the Ruby query:
      InvestmentUnMatch
        .joins(investment_transactions, instruments)
        .where(quantity > 0, status IN pending_statuses)
        .select(investment_un_matches.id, instruments.symbol)
    """
    sql = text("""
        SELECT
            ium.id                  AS unmatched_id,
            inst.id                 AS instrument_id,
            inst.symbol             AS symbol,
            inst.instrument_token   AS instrument_token,
            inst.strike_price             AS strike_price,
            inst.expiry_date             AS expiry_date,
            inst.lot_size           AS lot_size,
            inst.tick_size          AS tick_size,
            inst.is_active          AS is_active
        FROM investment_un_matches ium
        INNER JOIN investment_transactions txn
            ON txn.id = ium.investment_transaction_id
        INNER JOIN instruments inst
            ON inst.id = txn.instrument_id
        WHERE ium.quantity > 0
          AND ium.status IN :statuses
        ORDER BY txn.txn_date DESC
    """)

    df = pd.read_sql(sql, db.bind, params={"statuses": tuple(MATCH_STATUS_ALL_PENDING)})

    logger.info(
        "Found %d open-trade rows (%d unique instruments)",
        len(df), df["instrument_id"].nunique()
    )
    return df


# ── 2. CSV instrument master ───────────────────────────────────────────────────

def load_csv_instruments() -> pd.DataFrame:
    """
    Load instrument master from resources/instrument_list.csv and normalise types.

    Expected columns (standard Kite export):
      instrument_token, exchange_token, tradingsymbol, name,
      current_price, expiry_date, strike_price, tick_size, lot_size,
      instrument_type, segment, exchange
    """
    logger.info("Reading %s …", INSTRUMENTS_CSV)
    df = pd.read_csv(INSTRUMENTS_CSV, low_memory=False)

    df["instrument_token"] = pd.to_numeric(
        df["instrument_token"], errors="coerce"
    ).astype("Int64")

    df["strike_price"] = pd.to_numeric(df["strike"], errors="coerce")

    df["expiry_date"] = pd.to_datetime(df["expiry"], errors="coerce").dt.date

    logger.info("Loaded %d instruments from CSV", len(df))
    return df


def resolve_instrument(symbol: str, csv_df: pd.DataFrame) -> Optional[pd.Series]:
    """
    Find the best matching row for `symbol` from the CSV.

    Match order:
      1. Exact tradingsymbol match
      2. Prefix match on tradingsymbol (fallback)

    Exchange priority: NFO > BFO > NSE > BSE > others
    Among multiple expiries: pick the latest (most relevant for open trades).
    """
    matches = csv_df[csv_df["tradingsymbol"] == symbol]

    if matches.empty:
        matches = csv_df[csv_df["tradingsymbol"].str.startswith(symbol, na=False)]

    if matches.empty:
        return None

    exchange_order = {"NFO": 0, "BFO": 1, "NSE": 2, "BSE": 3}
    matches = matches.copy()
    matches["_exch_rank"] = matches["exchange"].map(exchange_order).fillna(99)
    matches = matches.sort_values(["_exch_rank", "expiry_date"], ascending=[True, False])

    return matches.iloc[0]


# ── 3. Resolve + apply updates ─────────────────────────────────────────────────

def build_updates(open_trades: pd.DataFrame, csv_df: pd.DataFrame) -> list[dict]:
    """
    For each unique instrument in open trades, resolve from CSV and build
    an update record for any field that has changed:
      instrument_token, strike_price, lot_size, tick_size, expiry_date
    """
    updates = []
    seen = set()

    for _, row in open_trades.iterrows():
        inst_id = int(row["instrument_id"])
        if inst_id in seen:
            continue
        seen.add(inst_id)

        matched = resolve_instrument(row["symbol"], csv_df)

        if matched is None:
            logger.warning(
                "No CSV match for symbol '%s' (instrument_id=%d)",
                row["symbol"], inst_id
            )
            continue

        # ── values from CSV ──────────────────────────────────────────────
        new_token    = int(matched["instrument_token"])   if pd.notna(matched["instrument_token"])   else None
        new_strike_price   = float(matched["strike_price"])           if pd.notna(matched.get("strike_price"))         else None
        new_lot_size = int(matched["lot_size"])           if pd.notna(matched.get("lot_size"))       else None
        new_tick_size= float(matched["tick_size"])        if pd.notna(matched.get("tick_size"))      else None
        new_expiry_date   = matched["expiry_date"]                  if pd.notna(matched.get("expiry_date"))         else None

        # ── current values from DB ───────────────────────────────────────
        old_token    = int(row["instrument_token"])  if pd.notna(row["instrument_token"]) else None
        old_strike_price   = float(row["strike_price"])          if pd.notna(row["strike_price"])           else None
        old_lot_size = int(row["lot_size"])          if pd.notna(row["lot_size"])         else None
        old_tick_size= float(row["tick_size"])       if pd.notna(row["tick_size"])        else None
        old_expiry_date   = row["expiry_date"]                 if pd.notna(row["expiry_date"])           else None

        # Skip entirely if nothing changed
        if (
            new_token    == old_token    and
            new_strike_price   == old_strike_price   and
            new_lot_size == old_lot_size and
            new_tick_size== old_tick_size and
            new_expiry_date   == old_expiry_date
        ):
            logger.debug("  ✓  %s already correct — skipping", row["symbol"])
            continue

        updates.append({
            "instrument_id": inst_id,
            "symbol":        row["symbol"],
            "new_token":     new_token,
            "new_strike_price":    new_strike_price,
            "new_lot_size":  new_lot_size,
            "new_tick_size": new_tick_size,
            "new_expiry_date":    new_expiry_date,
        })

        # Log each changed field clearly
        changes = []
        if new_token    != old_token:    changes.append(f"token {old_token} → {new_token}")
        if new_strike_price   != old_strike_price:   changes.append(f"strike_price {old_strike_price} → {new_strike_price}")
        if new_lot_size != old_lot_size: changes.append(f"lot_size {old_lot_size} → {new_lot_size}")
        if new_tick_size!= old_tick_size:changes.append(f"tick_size {old_tick_size} → {new_tick_size}")
        if new_expiry_date   != old_expiry_date:   changes.append(f"expiry_date {old_expiry_date} → {new_expiry_date}")
        logger.info("  %s (id=%d): %s", row["symbol"], inst_id, " | ".join(changes))

    return updates


def apply_instrument_updates(db: Session, updates: list[dict], dry_run: bool):
    """Write corrected instrument_token, strike_price, lot_size, tick_size, expiry_date back to instruments table."""
    if not updates:
        logger.info("No instrument data changes needed.")
        return

    if dry_run:
        logger.info("[DRY RUN] Would update %d instruments — skipping DB write", len(updates))
        return

    sql = text("""
        UPDATE instruments
        SET instrument_token = :new_token,
            strike_price           = :new_strike_price,
            lot_size         = :new_lot_size,
            tick_size        = :new_tick_size,
            expiry_date           = :new_expiry_date,
            updated_at       = NOW()
        WHERE id = :instrument_id
    """)

    db.execute(sql, updates)
    logger.info("Updated %d instruments (token, strike_price, lot_size, tick_size, expiry_date)", len(updates))


# ── 4. Deactivate expired instruments ─────────────────────────────────────────

def mark_expired_instruments_inactive(db: Session, dry_run: bool):
    """
    Set is_active = FALSE for all instruments where expiry_date < today
    and currently marked active.
    """
    today = date.today()

    check_sql = text("""
        SELECT id, symbol, expiry_date
        FROM instruments
        WHERE expiry_date < :today
          AND is_active = TRUE
    """)

    expired_df = pd.read_sql(check_sql, db.bind, params={"today": today})

    if expired_df.empty:
        logger.info("No active instruments with past expiry_date — nothing to deactivate")
        return

    logger.info("Found %d active instruments with expiry_date < %s:", len(expired_df), today)
    for _, r in expired_df.iterrows():
        logger.info("  id=%-6s  %-30s  expiry_date=%s", r["id"], r["symbol"], r["expiry_date"])

    if dry_run:
        logger.info(
            "[DRY RUN] Would mark %d instruments inactive — skipping DB write",
            len(expired_df)
        )
        return

    deactivate_sql = text("""
        UPDATE instruments
        SET is_active  = FALSE,
            updated_at = NOW()
        WHERE expiry_date < :today
          AND is_active = TRUE
    """)

    result = db.execute(deactivate_sql, {"today": today})
    logger.info("Marked %d expired instruments as inactive", result.rowcount)



# ── 5. Live price update (Futures + Equity) ────────────────────────────────────

# Instrument types that need live LTP — options are excluded because
# their price is tracked via P&L, not current_price on the instrument row.
LIVE_PRICE_TYPES = {"FUT", "EQ", "CE", "PE"}

# Kite LTP supports up to 500 instruments per call
KITE_LTP_BATCH_SIZE = 500


def fetch_and_update_live_prices(kite, db: Session, open_trades: pd.DataFrame, dry_run: bool):
    """
    For each open-trade instrument that is a FUTURE or EQUITY:
      1. Build the exchange:tradingsymbol key Kite expects
      2. Batch-call kite.ltp() (max 500 per call)
      3. Write the returned LTP back to instruments.current_price
    """
    # Pull instrument_type from the CSV-resolved data via the DB
    # (instrument_type column must exist on the instruments table)
    inst_sql = text("""
        SELECT
            inst.id             AS instrument_id,
            inst.symbol         AS symbol,
            inst.exchange       AS exchange,
            inst.instrument_type AS instrument_type,
            inst.current_price     AS current_price,
            inst.instrument_token     AS instrument_token
        FROM instruments inst
        WHERE inst.id IN :ids
          AND inst.is_active = TRUE
    """)

    instrument_ids = tuple(open_trades["instrument_id"].unique().tolist())
    rows = pd.read_sql(inst_sql, db.bind, params={"ids": instrument_ids})

    # Filter to only FUTURES and EQUITY
    eligible = rows[rows["instrument_type"].isin(LIVE_PRICE_TYPES)].copy()

    if eligible.empty:
        logger.info("No FUTURES or EQUITY instruments in open trades — skipping live price update")
        return

    # Build Kite-format keys: "NFO:NIFTY25MAYFUT", "NSE:RELIANCE" etc.
    eligible["kite_key"] = eligible["instrument_token"]
    kite_keys = eligible["kite_key"].tolist()

    logger.info(
        "Fetching live LTP from Kite for %d instruments (%s) …",
        len(kite_keys),
        ", ".join(eligible["instrument_type"].unique())
    )

    # Batch into chunks of 500
    ltp_map: dict = {}
    for i in range(0, len(kite_keys), KITE_LTP_BATCH_SIZE):
        batch = kite_keys[i : i + KITE_LTP_BATCH_SIZE]
        try:
            result = kite.ltp(batch)
            ltp_map.update(result)
        except Exception as exc:
            logger.error("kite.ltp() failed for batch starting at index %d: %s", i, exc)

    if not ltp_map:
        logger.warning("No LTP data returned from Kite — skipping price update")
        return

    # Build update list
    price_updates = []
    for _, row in eligible.iterrows():
        ltp_data = ltp_map.get(str(row["kite_key"]))
        if ltp_data is None:
            logger.warning("No LTP returned for %s — skipping", row["kite_key"])
            continue

        new_price = ltp_data.get("last_price")
        if new_price is None:
            logger.warning("LTP response missing current_price for %s — skipping", row["kite_key"])
            continue

        old_price = float(row["current_price"]) if pd.notna(row["current_price"]) else None

        logger.info(
            "  %s  [%s]  current_price  %s → %.2f",
            row["symbol"], row["instrument_type"], old_price, new_price
        )

        price_updates.append({
            "instrument_id": int(row["instrument_id"]),
            "new_price":     float(new_price),
        })

    if not price_updates:
        logger.info("No price updates to apply.")
        return

    if dry_run:
        logger.info("[DRY RUN] Would update current_price for %d instruments — skipping DB write", len(price_updates))
        return

    price_sql = text("""
        UPDATE instruments
        SET current_price = :new_price,
            updated_at = NOW()
        WHERE id = :instrument_id
    """)

    db.execute(price_sql, price_updates)
    logger.info("Updated current_price for %d instruments", len(price_updates))




def main():
    parser = argparse.ArgumentParser(description="Instrument sync — fix tokens, strike_prices, deactivate expired")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would change without writing to DB"
    )
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("Instrument Sync  |  dry_run=%s", args.dry_run)
    logger.info("=" * 60)

    # Kite token — same pattern as kite_sync.py
    # kite is available here if needed for future instrument lookups
    kite, user_id = fetch_user_token(logger)

    # DB session — same pattern as kite_sync.py
    with get_db() as db:

        # Step 1 — fetch open trades
        logger.info("\n[1/5] Fetching open-trade instruments from DB …")
        open_trades = fetch_open_trade_instruments(db)

        if open_trades.empty:
            logger.info("No open trades found. Nothing to sync.")
            return

        # Step 2 — load CSV instrument master
        logger.info("\n[2/5] Loading instrument master from CSV …")
        csv_df = load_csv_instruments()

        # Step 3 — resolve + update token / strike_price
        logger.info("\n[3/5] Resolving and updating instrument_token + strike_price …")
        updates = build_updates(open_trades, csv_df)
        apply_instrument_updates(db, updates, dry_run=args.dry_run)

        # Step 4 — deactivate expired instruments
        logger.info("\n[4/5] Marking expired instruments inactive …")
        mark_expired_instruments_inactive(db, dry_run=args.dry_run)

        # Step 5 — live LTP update for futures + equity
        logger.info("\n[5/5] Updating live prices for FUTURES and EQUITY …")
        fetch_and_update_live_prices(kite, db, open_trades, dry_run=args.dry_run)

    logger.info("\n" + "=" * 60)
    logger.info("Instrument sync complete%s", " (DRY RUN — nothing written)" if args.dry_run else "")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()