#!/usr/bin/env python3
"""
kite_market_fetcher.py
Fetches Nifty / BankNifty / VIX market data via Kite Connect and writes
MarketSnapshot + InstrumentWatchlist live-price rows to the shared PostgreSQL DB.

Runs via APScheduler every 60 seconds during market hours.
Project path: /Users/abhinavgarg/Documents/Projects/kiteConnect/

Requirements:
  pip install kiteconnect apscheduler pandas pandas-ta python-dotenv sqlalchemy
"""

import json
import logging
from datetime import datetime, timedelta
from zoneinfo  import ZoneInfo
from typing    import Optional

import pandas as pd
from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv         import load_dotenv

from engines.sr_engine import compute_sr_levels

load_dotenv()
from sqlalchemy     import text
from sqlalchemy.orm import Session

from trading.database   import get_db
from trading.user_token import fetch_user_token

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
# Tokens used for historical_data() calls
INSTRUMENT_TOKENS = {
    "NIFTY 50":   256265,
    "NIFTY BANK": 260105,
    "INDIA VIX":  264969,
}

# Quote keys used for kite.quote() — format is "EXCHANGE:TRADINGSYMBOL"
# kite.quote() does NOT accept instrument tokens, only trading symbols
QUOTE_KEYS = {
    "NIFTY 50":   "NSE:NIFTY 50",
    "NIFTY BANK": "NSE:NIFTY BANK",
    "INDIA VIX":  "NSE:INDIA VIX",
}

# Days of daily candles fetched to warm up sr_engine for live S/R
SR_DAILY_LOOKBACK_DAYS    = 60
# Days of 15-min candles for volume profile
SR_INTRADAY_LOOKBACK_DAYS = 30


# ── DB helpers — all accept SQLAlchemy Session ────────────────────────────────

def insert_market_snapshot(db: Session, data: dict) -> None:
    """Insert a new snapshot row (keeps full history for chart sparklines)."""
    cols = ", ".join(data.keys())
    ph   = ", ".join(f":{k}" for k in data.keys())
    db.execute(text(f"INSERT INTO market_snapshots ({cols}) VALUES ({ph})"), data)
    db.commit()


def update_watchlist_prices(
    db:               Session,
    symbol:           str,
    ltp:              float,
    day_pct:          float,
    rsi:              Optional[float],
    vol_ratio:        Optional[float],
) -> None:
    db.execute(
        text("""
            UPDATE instrument_watchlists
               SET ltp               = :ltp,
                   day_change_pct    = :day_pct,
                   rsi_14            = :rsi,
                   volume_ratio      = :vol_ratio,
                   last_refreshed_at = NOW()
             WHERE symbol = :symbol
        """),
        {
            "ltp":      ltp,
            "day_pct":  day_pct,
            "rsi":      rsi,
            "vol_ratio":vol_ratio,
            "symbol":   symbol,
        }
    )
    db.commit()


def get_active_watchlist(db: Session) -> list[dict]:
    result = db.execute(text("""
        SELECT id, symbol, exchange, support_level, resistance_level
          FROM instrument_watchlists
         WHERE status NOT IN ('exited')
    """))
    cols = list(result.keys())
    return [dict(zip(cols, row)) for row in result.fetchall()]


# ── Technical indicators ──────────────────────────────────────────────────────

def _safe(val) -> Optional[float]:
    try:
        f = float(val)
        return None if (f != f or abs(f) == float("inf")) else f
    except Exception:
        return None


def compute_indicators(candles: pd.DataFrame) -> dict:
    """
    Compute intraday indicators from 5-min candle DataFrame.
    Returns dict of latest values.

    VWAP is reset to today's session only (9:15 AM onwards).
    The full 2-day candle history is kept so RSI/EMA have enough bars,
    but VWAP cumsum only runs over today's candles.
    """
    c = candles["close"]
    v = candles["volume"]

    if USE_TALIB:
        rsi   = talib.RSI(c, timeperiod=14).iloc[-1]
        ema20 = talib.EMA(c, timeperiod=20).iloc[-1]
        ema50 = talib.EMA(c, timeperiod=50).iloc[-1]
    else:
        rsi   = pta.rsi(c, length=14).iloc[-1]
        ema20 = pta.ema(c, length=20).iloc[-1]
        ema50 = pta.ema(c, length=50).iloc[-1]

    avg_vol = v.tail(20).mean()

    # VWAP resets at market open each day — filter to today's session only
    if "date" not in candles.columns:
        # historical_data returns 'date' key; ensure column exists
        today_candles = candles
    else:
        today_date    = pd.Timestamp.now(tz="Asia/Kolkata").date()
        today_candles = candles[
            pd.to_datetime(candles["date"]).dt.tz_convert("Asia/Kolkata").dt.date == today_date
        ]

    if today_candles.empty:
        # Fallback: use all candles (e.g. called after hours)
        today_candles = candles

    tv = today_candles["volume"]
    typical_price = (today_candles["high"] + today_candles["low"] + today_candles["close"]) / 3
    cumvol        = tv.cumsum()
    vwap_series   = (typical_price * tv).cumsum() / cumvol
    vwap          = vwap_series.iloc[-1] if not vwap_series.empty else float("nan")

    return {
        "rsi_14":        _safe(rsi),
        "ema_20":        _safe(ema20),
        "ema_50":        _safe(ema50),
        "vwap":          _safe(vwap),
        "volume":        int(tv.iloc[-1]) if not today_candles.empty else int(v.iloc[-1]),
        "avg_volume_20d":int(avg_vol) if avg_vol == avg_vol else None,
    }


def compute_mood_score(day_pct, week_pct, month_pct, rsi, above_vwap, vol_ratio) -> int:
    score = 0
    score += max(min((day_pct   or 0) * 4,  20), -20)
    score += max(min((week_pct  or 0) * 2,  10), -10)
    score += max(min((month_pct or 0) * 1,  10), -10)
    if rsi:
        score += max(min((rsi - 50) * 0.5, 25), -25)
    score += 15 if above_vwap else -15
    if vol_ratio and vol_ratio > 1.5:
        score += 10 if (day_pct or 0) > 0 else -10
    return max(min(int(score), 100), -100)


def mood_label_from_score(score: int) -> str:
    if score >= 60:  return "Strong Bull"
    if score >= 20:  return "Bull"
    if score >= -19: return "Neutral"
    if score >= -59: return "Bear"
    return "Strong Bear"


def trend_direction(score: int) -> str:
    if score >= 20:  return "bullish"
    if score <= -20: return "bearish"
    return "sideways"


def generate_trade_signals(snap: dict) -> list:
    signals = []
    ltp  = snap.get("ltp", 0)
    rsi  = snap.get("rsi_14")
    vwap = snap.get("vwap")
    s1   = snap.get("support_1")
    r1   = snap.get("resistance_1")

    if not all([ltp, rsi, vwap, s1, r1]):
        return signals

    if abs(ltp - s1) / s1 < 0.005 and 30 <= rsi <= 45:
        sl     = round(s1 * 0.995, 2)
        target = round(ltp + (ltp - sl) * 2, 2)
        signals.append({
            "direction": "long", "strategy": "Support Bounce", "strength": "moderate",
            "entry": ltp, "stop_loss": sl, "target": target,
            "notes": f"Price at S1 ({s1}), RSI recovering from oversold",
        })

    if ltp > vwap and snap.get("day_change_pct", 0) > 0.3:
        sl     = round(vwap * 0.998, 2)
        target = round(ltp + (ltp - sl) * 1.5, 2)
        signals.append({
            "direction": "long", "strategy": "VWAP Reclaim", "strength": "moderate",
            "entry": ltp, "stop_loss": sl, "target": target,
            "notes": f"Price reclaimed VWAP ({vwap})",
        })

    if abs(ltp - r1) / r1 < 0.004 and rsi > 65:
        sl     = round(r1 * 1.005, 2)
        target = round(ltp - (sl - ltp) * 2, 2)
        signals.append({
            "direction": "short", "strategy": "Resistance Rejection", "strength": "moderate",
            "entry": ltp, "stop_loss": sl, "target": target,
            "notes": f"Price at R1 ({r1}), RSI overbought",
        })

    return signals


# ── Main fetch + write job ────────────────────────────────────────────────────

def fetch_and_write():
    now = datetime.now(IST)

    if now.weekday() >= 5:
        log.info("Weekend — skipping fetch")
        return
    if not (9 * 60 + 15 <= now.hour * 60 + now.minute <= 15 * 60 + 35):
        log.info("Outside market hours — skipping fetch")
        return

    try:
        kite, user_id = fetch_user_token(log)
    except Exception as e:
        log.error(f"fetch_user_token failed: {e}")
        return

    try:
        with get_db() as db:
            _run_fetch(kite, db, now)
    except Exception as e:
        log.error(f"fetch_and_write error: {e}", exc_info=True)


def _run_fetch(kite, db: Session, now: datetime) -> None:
    """Core fetch logic — separated so get_db() context wraps the whole run."""

    # ── NIFTY 50 — full indicator + S/R fetch ────────────────────────────────
    token     = INSTRUMENT_TOKENS["NIFTY 50"]
    quote_key = QUOTE_KEYS["NIFTY 50"]
    quote     = kite.quote([quote_key])[quote_key]
    ohlc    = quote["ohlc"]

    from_5min = (now - timedelta(days=2)).replace(hour=9, minute=15, second=0, microsecond=0)
    candles   = kite.historical_data(token, from_5min, now, "5minute")
    df_5min   = pd.DataFrame(candles)

    if df_5min.empty:
        log.warning("No 5-min candle data for NIFTY 50 — skipping run")
        return

    indicators = compute_indicators(df_5min)

    ltp        = float(quote["last_price"])
    prev_close = float(ohlc["close"])
    day_pct    = round((ltp - prev_close) / prev_close * 100, 2)

    # Week / month % from daily candles
    daily = kite.historical_data(token, now - timedelta(days=35), now, "day")
    df_d  = pd.DataFrame(daily)
    week_pct  = round((ltp - float(df_d["close"].iloc[-6]))  / float(df_d["close"].iloc[-6])  * 100, 2) if len(df_d) >= 6  else None
    month_pct = round((ltp - float(df_d["close"].iloc[-22])) / float(df_d["close"].iloc[-22]) * 100, 2) if len(df_d) >= 22 else None

    # S/R via sr_engine — daily + 15-min for confluence
    df_daily_sr = pd.DataFrame(
        kite.historical_data(
            token,
            now - timedelta(days=SR_DAILY_LOOKBACK_DAYS),
            now, "day"
        )
    )
    df_daily_sr["date"] = pd.to_datetime(df_daily_sr["date"])

    df_15min_sr = pd.DataFrame(
        kite.historical_data(
            token,
            now - timedelta(days=SR_INTRADAY_LOOKBACK_DAYS),
            now, "15minute"
        )
    )
    if not df_15min_sr.empty:
        df_15min_sr["date"] = pd.to_datetime(df_15min_sr["date"])

    sr_levels = compute_sr_levels(
        daily_df    = df_daily_sr,
        intraday_df = df_15min_sr if not df_15min_sr.empty else pd.DataFrame(),
        ref_price   = ltp,
    )

    above_vwap = ltp > indicators["vwap"] if indicators["vwap"] else False
    vol_ratio  = round(indicators["volume"] / indicators["avg_volume_20d"], 2) if indicators["avg_volume_20d"] else None
    score      = compute_mood_score(day_pct, week_pct or 0, month_pct or 0,
                                    indicators["rsi_14"], above_vwap, vol_ratio)

    snap = {
        **indicators,
        "support_1":    sr_levels.get("support_1"),
        "support_2":    sr_levels.get("support_2"),
        "resistance_1": sr_levels.get("resistance_1"),
        "resistance_2": sr_levels.get("resistance_2"),
        "symbol":           "NIFTY 50",
        "ltp":              ltp,
        "open_price":       float(ohlc["open"]),
        "high_price":       float(ohlc["high"]),
        "low_price":        float(ohlc["low"]),
        "close_price":      prev_close,
        "day_change_pct":   day_pct,
        "week_change_pct":  week_pct,
        "month_change_pct": month_pct,
        "mood_score":       score,
        "mood_label":       mood_label_from_score(score),
        "trend_direction":  trend_direction(score),
        "trade_signals":    json.dumps(generate_trade_signals({
            "ltp": ltp, "rsi_14": indicators["rsi_14"],
            "vwap": indicators["vwap"], "day_change_pct": day_pct,
            **sr_levels,
        })),
        "vwap": indicators["vwap"],
        "captured_at": now,
        "created_at":  now,
        "updated_at":  now,
    }

    insert_market_snapshot(db, snap)
    log.info(f"Snapshot written — NIFTY 50 LTP={ltp} mood={score} "
             f"S1={sr_levels.get('support_1')} R1={sr_levels.get('resistance_1')}")

    # ── BankNifty + VIX — LTP + day% only ───────────────────────────────────
    for label in ["NIFTY BANK", "INDIA VIX"]:
        qkey = QUOTE_KEYS[label]
        q    = kite.quote([qkey])[qkey]
        l    = float(q["last_price"])
        pc   = float(q["ohlc"]["close"])
        dpct = round((l - pc) / pc * 100, 2)
        insert_market_snapshot(db, {
            "symbol":         label,
            "ltp":            l,
            "open_price":     float(q["ohlc"]["open"]),
            "high_price":     float(q["ohlc"]["high"]),
            "low_price":      float(q["ohlc"]["low"]),
            "close_price":    pc,
            "day_change_pct": dpct,
            "captured_at":    now,
            "created_at":     now,
            "updated_at":     now,
        })

    # ── Watchlist live price refresh ─────────────────────────────────────────
    for inst in get_active_watchlist(db):
        wsymbol   = inst["symbol"]
        w_support = inst.get("support_level")
        w_resist  = inst.get("resistance_level")

        try:
            wexchange = inst.get("exchange") or "NSE"
            wqkey     = f"{wexchange}:{wsymbol}"
            wq        = kite.quote([wqkey])
            wqd       = wq.get(wqkey, {})
            if not wqd:
                continue

            w_ltp  = float(wqd["last_price"])
            w_pc   = float(wqd["ohlc"]["close"])
            w_dpct = round((w_ltp - w_pc) / w_pc * 100, 2)

            wrsi = wvol_ratio = None
            try:
                wc  = kite.historical_data(
                    wqd["instrument_token"],
                    now - timedelta(days=2), now, "5minute"
                )
                wdf = pd.DataFrame(wc)
                if not wdf.empty and len(wdf) >= 15:
                    ind        = compute_indicators(wdf)
                    wrsi       = ind["rsi_14"]
                    wvol_ratio = round(ind["volume"] / ind["avg_volume_20d"], 2) if ind["avg_volume_20d"] else None
            except Exception as e:
                log.debug(f"  Indicator fetch skipped for {wsymbol}: {e}")

            pct_sup = round((w_ltp - float(w_support)) / float(w_support) * 100, 2) if w_support else None
            pct_res = round((float(w_resist) - w_ltp)  / float(w_resist)  * 100, 2) if w_resist  else None

            update_watchlist_prices(db, wsymbol, w_ltp, w_dpct, wrsi, wvol_ratio)

        except Exception as e:
            log.warning(f"Watchlist update failed for {wsymbol}: {e}")


# ── Scheduler entry point ─────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("Starting kite_market_fetcher (60s interval, market hours only)")
    fetch_and_write()   # run immediately on startup

    scheduler = BlockingScheduler(timezone=IST)
    scheduler.add_job(fetch_and_write, "interval", seconds=60, id="market_fetch")
    scheduler.start()