#!/usr/bin/env python3
"""
backfill_history.py
One-time (or re-runnable) script that fills market_snapshots and
instrument_watchlist historical price fields for the past 3 months
using daily Kite candles.

Run ONCE before starting kite_market_fetcher.py:
    python backfill_history.py

Safe to re-run — already-existing (symbol, captured_at::date) rows are
skipped via INSERT ... ON CONFLICT DO NOTHING.

Strategy for daily snapshots:
  - One row per trading day per symbol
  - captured_at = market close time for that day (15:30 IST)
  - RSI / EMA computed from the trailing window available up to that day
    (no look-ahead: for day N we only use candles 1..N)
  - VWAP set to NULL (intraday concept; not meaningful on daily bars)
  - week_change_pct  = % change vs 5 trading days prior
  - month_change_pct = % change vs 21 trading days prior
  - S/R computed via sr_engine (swing highs/lows + volume profile confluence)

Requirements:
    pip install kiteconnect pandas pandas-ta python-dotenv tqdm sqlalchemy
"""

import json
import logging
from contextlib import contextmanager
from datetime   import datetime, timedelta, date, time
from zoneinfo   import ZoneInfo
from typing     import Optional

import pandas as pd
from dotenv         import load_dotenv
load_dotenv()
from sqlalchemy     import text
from sqlalchemy.orm import Session

from engines.sr_engine import compute_sr_levels
from trading.database   import get_db
from trading.user_token import fetch_user_token

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(iterable, **kwargs):
        print(f"  {kwargs.get('desc', '')} ({kwargs.get('total', '?')} items)…")
        return iterable

try:
    import talib
    USE_TALIB = True
except ImportError:
    import pandas_ta as pta
    USE_TALIB = False

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# ── Config ────────────────────────────────────────────────────────────────────
MARKET_CLOSE          = time(15, 30)
INDICATOR_WARMUP_DAYS = 80    # extra pre-window days so EMA-50/RSI-14 are warm
INTRADAY_WINDOW_DAYS  = 60    # 15-min candle window for volume profile S/R

INDEX_INSTRUMENTS = {
    "NIFTY 50":   256265,
    "NIFTY BANK": 260105,
    "INDIA VIX":  264969,
}


# ── DB helpers — all accept a SQLAlchemy Session ──────────────────────────────

def already_backfilled_dates(db: Session, symbol: str) -> set:
    """Return set of date objects already present in market_snapshots for symbol."""
    result = db.execute(
        text("""
            SELECT captured_at::date
            FROM market_snapshots
            WHERE symbol = :symbol
        """),
        {"symbol": symbol}
    )
    return {row[0] for row in result.fetchall()}


def insert_snapshots_batch(db: Session, rows: list[dict]) -> None:
    """
    Bulk insert snapshot rows, skipping duplicates via ON CONFLICT DO NOTHING.
    Chunked to stay within SQLAlchemy bind-parameter limits.
    """
    if not rows:
        return

    CHUNK   = 200
    columns = list(rows[0].keys())
    col_str = ", ".join(columns)

    for chunk_start in range(0, len(rows), CHUNK):
        chunk = rows[chunk_start : chunk_start + CHUNK]

        placeholders = []
        params: dict = {}
        for i, row in enumerate(chunk):
            row_ph = ", ".join(f":{col}_{i}" for col in columns)
            placeholders.append(f"({row_ph})")
            for col in columns:
                params[f"{col}_{i}"] = row[col]

        sql = (
            f"INSERT INTO market_snapshots ({col_str}) "
            f"VALUES {', '.join(placeholders)} "
            f"ON CONFLICT DO NOTHING"
        )
        db.execute(text(sql), params)

    db.commit()


def get_watchlist_instruments(db: Session) -> list[dict]:
    """Return all non-exited instruments with their user-set levels."""
    result = db.execute(text("""
        SELECT id, symbol, exchange, instrument_type,
               support_level, resistance_level
          FROM instrument_watchlists
         WHERE status != 'exited'
    """))
    cols = list(result.keys())
    return [dict(zip(cols, row)) for row in result.fetchall()]


def save_watchlist_backtest_summary(db: Session, instrument_id: int, summary: dict) -> None:
    db.execute(
        text("""
            UPDATE instrument_watchlists
               SET backtest_results = :results
             WHERE id = :id
        """),
        {"results": json.dumps(summary), "id": instrument_id}
    )
    db.commit()


# ── Indicator helpers ─────────────────────────────────────────────────────────

def _safe(val) -> Optional[float]:
    """Return float or None for NaN / inf."""
    try:
        f = float(val)
        return None if (f != f or abs(f) == float("inf")) else f
    except Exception:
        return None


def compute_daily_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute rolling RSI-14, EMA-20, EMA-50, avg_volume_20d for every row.
    VWAP intentionally omitted — not meaningful on daily bars.
    """
    df = df.copy()
    c  = df["close"]
    v  = df["volume"]

    if USE_TALIB:
        df["rsi_14"] = talib.RSI(c, timeperiod=14)
        df["ema_20"] = talib.EMA(c, timeperiod=20)
        df["ema_50"] = talib.EMA(c, timeperiod=50)
    else:
        df["rsi_14"] = pta.rsi(c, length=14)
        df["ema_20"] = pta.ema(c, length=20)
        df["ema_50"] = pta.ema(c, length=50)

    df["avg_volume_20d"] = v.rolling(20).mean().round(0)
    return df


def compute_mood_score(day_pct, week_pct, month_pct, rsi, vol_ratio) -> int:
    score = 0
    score += max(min((day_pct   or 0) * 4, 20), -20)
    score += max(min((week_pct  or 0) * 2, 10), -10)
    score += max(min((month_pct or 0) * 1, 10), -10)
    if rsi:
        score += max(min((rsi - 50) * 0.5, 25), -25)
    if vol_ratio and vol_ratio > 1.5:
        score += 10 if (day_pct or 0) > 0 else -10
    return max(min(int(score), 100), -100)


def mood_label(score: int) -> str:
    if score >= 60:  return "Strong Bull"
    if score >= 20:  return "Bull"
    if score >= -19: return "Neutral"
    if score >= -59: return "Bear"
    return "Strong Bear"


def trend_dir(score: int) -> str:
    if score >= 20:  return "bullish"
    if score <= -20: return "bearish"
    return "sideways"


# ── Candle fetch helpers ──────────────────────────────────────────────────────

def fetch_daily_candles(kite, token: int, from_date: date, to_date: date) -> pd.DataFrame:
    """Fetch daily candles from Kite and return sorted DataFrame."""
    candles = kite.historical_data(
        token,
        datetime.combine(from_date, time(9, 15)).replace(tzinfo=IST),
        datetime.combine(to_date,   time(15, 30)).replace(tzinfo=IST),
        "day"
    )
    if not candles:
        return pd.DataFrame()
    df = pd.DataFrame(candles)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def fetch_intraday_candles(kite, token: int, from_date: date, to_date: date) -> pd.DataFrame:
    """Fetch 15-min candles (max 60 days) for volume profile S/R."""
    try:
        candles = kite.historical_data(
            token,
            datetime.combine(from_date, time(9, 15)).replace(tzinfo=IST),
            datetime.combine(to_date,   time(15, 30)).replace(tzinfo=IST),
            "15minute"
        )
    except Exception as e:
        log.warning(f"  15-min candle fetch failed: {e}")
        return pd.DataFrame()
    if not candles:
        return pd.DataFrame()
    df = pd.DataFrame(candles)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


# ── Row builder ───────────────────────────────────────────────────────────────

def build_snapshot_rows(
    symbol:         str,
    df:             pd.DataFrame,
    backfill_from:  date,
    backfill_to:    date,
    existing_dates: set,
    sr_levels:      Optional[dict] = None,
) -> list[dict]:
    """
    Walk df (sorted ascending, full warmup window included) and return
    one snapshot dict per trading day within [backfill_from, backfill_to]
    that isn't already in the DB.

    sr_levels: result of compute_sr_levels() — same S/R applied to all rows
               for a given symbol (computed once from the full window).
    """
    rows = []

    for idx in range(len(df)):
        row      = df.iloc[idx]
        row_date = pd.Timestamp(row["date"]).date()

        if row_date < backfill_from or row_date > backfill_to:
            continue
        if row_date in existing_dates:
            log.debug(f"  Skipping {symbol} {row_date} — already in DB")
            continue

        close      = float(row["close"])
        prev_close = float(df.iloc[idx - 1]["close"]) if idx > 0 else float(row["open"])
        day_pct    = round((close - prev_close) / prev_close * 100, 2) if prev_close else 0

        week_pct  = None
        if idx >= 5:
            wc = float(df.iloc[idx - 5]["close"])
            week_pct = round((close - wc) / wc * 100, 2) if wc else None

        month_pct = None
        if idx >= 21:
            mc = float(df.iloc[idx - 21]["close"])
            month_pct = round((close - mc) / mc * 100, 2) if mc else None

        # S/R from sr_engine — computed once per symbol, applied to all rows
        sr = {
            "support_1":    sr_levels.get("support_1")    if sr_levels else None,
            "support_2":    sr_levels.get("support_2")    if sr_levels else None,
            "resistance_1": sr_levels.get("resistance_1") if sr_levels else None,
            "resistance_2": sr_levels.get("resistance_2") if sr_levels else None,
        }

        rsi       = _safe(row.get("rsi_14"))
        ema20     = _safe(row.get("ema_20"))
        ema50     = _safe(row.get("ema_50"))
        avg_v     = _safe(row.get("avg_volume_20d"))
        volume    = int(row["volume"])
        vol_ratio = round(volume / avg_v, 2) if avg_v and avg_v > 0 else None

        score  = compute_mood_score(day_pct, week_pct, month_pct, rsi, vol_ratio)
        cap_at = datetime.combine(row_date, MARKET_CLOSE).replace(tzinfo=IST)

        rows.append({
            "symbol":           symbol,
            "ltp":              close,
            "open_price":       float(row["open"]),
            "high_price":       float(row["high"]),
            "low_price":        float(row["low"]),
            "close_price":      prev_close,
            "day_change_pct":   day_pct,
            "week_change_pct":  week_pct,
            "month_change_pct": month_pct,
            **sr,
            "vwap":             None,
            "ema_20":           ema20,
            "ema_50":           ema50,
            "rsi_14":           rsi,
            "volume":           volume,
            "avg_volume_20d":   int(avg_v) if avg_v else None,
            "mood_score":       score,
            "mood_label":       mood_label(score),
            "trend_direction":  trend_dir(score),
            "trade_signals":    json.dumps([]),
            "captured_at":      cap_at,
            "created_at":       cap_at,
            "updated_at":       cap_at,
        })

    return rows


def resolve_instrument_token(kite, symbol: str, exchange: str) -> Optional[int]:
    """Look up the Kite instrument token for a watchlist symbol."""
    try:
        for inst in kite.instruments(exchange):
            if inst["tradingsymbol"] == symbol and inst["segment"] == exchange:
                return inst["instrument_token"]
        log.warning(f"  Token not found for {exchange}:{symbol}")
        return None
    except Exception as e:
        log.error(f"  Token lookup failed for {symbol}: {e}")
        return None


# ── Phase 1: Index backfill ───────────────────────────────────────────────────

def backfill_indices(kite, db: Session, backfill_from: date, backfill_to: date):
    log.info("=" * 60)
    log.info("PHASE 1 — Index snapshots: NIFTY 50 / BANK NIFTY / VIX")
    log.info("=" * 60)

    fetch_from    = backfill_from - timedelta(days=INDICATOR_WARMUP_DAYS)
    intraday_from = backfill_to   - timedelta(days=INTRADAY_WINDOW_DAYS)

    for symbol, token in tqdm(INDEX_INSTRUMENTS.items(), desc="Indices", total=3):
        log.info(f"  [{symbol}] Fetching daily candles from {fetch_from} …")
        df = fetch_daily_candles(kite, token, fetch_from, backfill_to)
        if df.empty:
            log.warning(f"  [{symbol}] No data — skipping")
            continue

        log.info(f"  [{symbol}] {len(df)} candles. Computing indicators…")
        df = compute_daily_indicators(df)

        log.info(f"  [{symbol}] Fetching 15-min candles for volume profile…")
        df_15min = fetch_intraday_candles(kite, token, intraday_from, backfill_to)

        ref_price = float(df[df["date"].dt.date <= backfill_to]["close"].iloc[-1])
        log.info(f"  [{symbol}] Computing S/R levels (ref={ref_price})…")
        sr_levels = compute_sr_levels(
            daily_df    = df,
            intraday_df = df_15min,
            ref_price   = ref_price,
        )
        log.info(
            f"  [{symbol}] S/R → "
            f"S1={sr_levels['support_1']} S2={sr_levels['support_2']} "
            f"R1={sr_levels['resistance_1']} R2={sr_levels['resistance_2']} "
            f"({len(sr_levels.get('sr_levels_detail', []))} raw levels)"
        )

        existing = already_backfilled_dates(db, symbol)
        log.info(f"  [{symbol}] {len(existing)} dates already in DB")

        rows = build_snapshot_rows(symbol, df, backfill_from, backfill_to, existing, sr_levels)
        log.info(f"  [{symbol}] {len(rows)} new rows to insert")

        if rows:
            insert_snapshots_batch(db, rows)
            log.info(f"  [{symbol}] ✓ Done")


# ── Phase 2: Watchlist backfill ───────────────────────────────────────────────

def backfill_watchlist(kite, db: Session, backfill_from: date, backfill_to: date):
    log.info("=" * 60)
    log.info("PHASE 2 — Watchlist instrument price history")
    log.info("=" * 60)

    instruments = get_watchlist_instruments(db)
    if not instruments:
        log.info("  No active watchlist instruments — skipping")
        return

    log.info(f"  {len(instruments)} instruments to process")
    fetch_from    = backfill_from - timedelta(days=INDICATOR_WARMUP_DAYS)
    intraday_from = backfill_to   - timedelta(days=INTRADAY_WINDOW_DAYS)

    for inst in tqdm(instruments, desc="Watchlist", total=len(instruments)):
        symbol      = inst["symbol"]
        exchange    = inst.get("exchange") or "NSE"
        support_lvl = inst.get("support_level")
        resist_lvl  = inst.get("resistance_level")
        inst_id     = inst["id"]

        log.info(f"  [{symbol}] Processing…")

        token = resolve_instrument_token(kite, symbol, exchange)
        if token is None:
            continue

        try:
            df = fetch_daily_candles(kite, token, fetch_from, backfill_to)
        except Exception as e:
            log.warning(f"  [{symbol}] Daily candle fetch failed: {e}")
            continue

        if df.empty:
            log.warning(f"  [{symbol}] No candle data — skipping")
            continue

        df = compute_daily_indicators(df)

        df_15min = fetch_intraday_candles(kite, token, intraday_from, backfill_to)

        ref_price = float(df[df["date"].dt.date <= backfill_to]["close"].iloc[-1])
        sr_levels = compute_sr_levels(
            daily_df    = df,
            intraday_df = df_15min,
            ref_price   = ref_price,
        )
        log.info(
            f"  [{symbol}] S/R → "
            f"S1={sr_levels['support_1']} S2={sr_levels['support_2']} "
            f"R1={sr_levels['resistance_1']} R2={sr_levels['resistance_2']}"
        )

        existing = already_backfilled_dates(db, symbol)
        rows     = build_snapshot_rows(symbol, df, backfill_from, backfill_to, existing, sr_levels)

        # Augment rows with pct_from_support / pct_from_resist
        # (using the user's currently configured levels as the historical reference)
        pct_support_list: list[Optional[float]] = []
        for r in rows:
            ltp = r["ltp"]
            pfs = (
                round((ltp - float(support_lvl)) / float(support_lvl) * 100, 2)
                if support_lvl and float(support_lvl) > 0 else None
            )
            pfr = (
                round((float(resist_lvl) - ltp) / float(resist_lvl) * 100, 2)
                if resist_lvl and float(resist_lvl) > 0 else None
            )
            # r["pct_from_support"] = pfs
            # r["pct_from_resist"]  = pfr
            pct_support_list.append(pfs)

        if rows:
            insert_snapshots_batch(db, rows)
            log.info(f"  [{symbol}] ✓ Inserted {len(rows)} rows")

        # Backtest summary
        valid_pcts = [p for p in pct_support_list if p is not None]
        if valid_pcts and support_lvl:
            near_days  = sum(1 for p in valid_pcts if 0   <= p <= 2.0)
            below_days = sum(1 for p in valid_pcts if p   < 0)

            backfill_df = df[df["date"].dt.date.between(backfill_from, backfill_to)].copy()
            backfill_df["day_ret"] = backfill_df["close"].pct_change() * 100
            positive_days = int((backfill_df["day_ret"] > 0).sum())
            total_days    = int(backfill_df["day_ret"].notna().sum())

            summary = {
                "backfill_from":        backfill_from.isoformat(),
                "backfill_to":          backfill_to.isoformat(),
                "total_trading_days":   len(rows),
                "near_support_days":    near_days,
                "below_support_days":   below_days,
                "positive_close_days":  positive_days,
                "win_rate_pct":         round(positive_days / total_days * 100, 1) if total_days else None,
                "min_pct_from_support": round(min(valid_pcts), 2),
                "max_pct_from_support": round(max(valid_pcts), 2),
                "avg_pct_from_support": round(sum(valid_pcts) / len(valid_pcts), 2),
                "support_level_used":   float(support_lvl),
                "sr_engine": {
                    "support_1":    sr_levels.get("support_1"),
                    "support_2":    sr_levels.get("support_2"),
                    "resistance_1": sr_levels.get("resistance_1"),
                    "resistance_2": sr_levels.get("resistance_2"),
                },
            }
            save_watchlist_backtest_summary(db, inst_id, summary)
            log.info(
                f"  [{symbol}] Backtest summary saved — "
                f"{near_days} near-support days, win rate {summary['win_rate_pct']}%"
            )


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    today = date.today()
    month = today.month - 3
    year  = today.year
    if month <= 0:
        month += 12
        year  -= 1
    bf_from = date(year, month, today.day)
    bf_to   = today

    log.info(f"Backfill range : {bf_from} → {bf_to}")
    log.info(f"Indicator warmup: {INDICATOR_WARMUP_DAYS} extra days before window")

    kite, user_id = fetch_user_token(log)
    log.info(f"Kite session ready — user_id={user_id}")

    with get_db() as db:
        backfill_indices(kite, db, bf_from, bf_to)
        backfill_watchlist(kite, db, bf_from, bf_to)

    log.info("=" * 60)
    log.info("Backfill complete.")
    log.info("=" * 60)


if __name__ == "__main__":
    main()