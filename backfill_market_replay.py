#!/usr/bin/env python3
"""Backfill completed 1m, 5m and 15m candles for the Rails market replay.

Usage:
    venv/bin/python backfill_market_replay.py --days 10 --symbol "NIFTY 50"

The command is safe to rerun: rows are upserted by
(symbol, timeframe, captured_at).
"""

import argparse
import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import text

from db_values import normalize_db_params
from indicators.calculate_basic_indicator import calculate_basic_indicators
from trading.database import get_db
from trading.user_token import fetch_user_token

IST = ZoneInfo("Asia/Kolkata")
INSTRUMENTS = {"NIFTY 50": 256265, "NIFTY BANK": 260105}
TIMEFRAMES = {"1m": "minute", "5m": "5minute", "15m": "15minute"}
log = logging.getLogger(__name__)


def safe(value, digits=2):
    return None if value is None or pd.isna(value) else round(float(value), digits)


def snapshot_rows(frame, symbol, timeframe):
    frame = calculate_basic_indicators(frame.copy())
    now = datetime.now(IST)
    rows = []
    for _, candle in frame.iterrows():
        captured_at = pd.Timestamp(candle["date"])
        if captured_at.tzinfo is None:
            captured_at = captured_at.tz_localize(IST)
        captured_at = captured_at.tz_convert("UTC").to_pydatetime()
        open_price, close_price = float(candle["open"]), float(candle["close"])
        rows.append(normalize_db_params({
            "symbol": symbol, "timeframe": timeframe, "ltp": close_price,
            "open_price": open_price, "high_price": float(candle["high"]),
            "low_price": float(candle["low"]), "close_price": close_price,
            "day_change_pct": round((close_price - open_price) / open_price * 100, 2),
            "vwap": safe(candle.get("vwap")), "ema_20": safe(candle.get("ema_20")),
            "ema_50": safe(candle.get("ema_50")), "rsi_14": safe(candle.get("rsi")),
            "trend_direction": "bullish" if close_price >= open_price else "bearish",
            "mood_label": "Neutral", "mood_score": 0,
            "volume": int(candle.get("volume", 0)), "avg_volume_20d": 0,
            "trade_signals": json.dumps({
                "adx_14": safe(candle.get("adx")),
                "plus_di": safe(candle.get("plus_di")),
                "minus_di": safe(candle.get("minus_di")),
                "macd_value": safe(candle.get("macd_line"), 4),
                "macd_signal": safe(candle.get("signal_line"), 4),
            }),
            "captured_at": captured_at, "created_at": now, "updated_at": now,
        }))
    return rows


def upsert(db, rows):
    statement = text("""
        INSERT INTO market_snapshots
          (symbol, timeframe, ltp, open_price, high_price, low_price, close_price,
           day_change_pct, vwap, ema_20, ema_50, rsi_14, trend_direction,
           mood_label, mood_score, volume, avg_volume_20d, trade_signals,
           captured_at, created_at, updated_at)
        VALUES
          (:symbol, :timeframe, :ltp, :open_price, :high_price, :low_price, :close_price,
           :day_change_pct, :vwap, :ema_20, :ema_50, :rsi_14, :trend_direction,
           :mood_label, :mood_score, :volume, :avg_volume_20d, :trade_signals,
           :captured_at, :created_at, :updated_at)
        ON CONFLICT (symbol, timeframe, captured_at) DO UPDATE SET
          ltp = EXCLUDED.ltp, open_price = EXCLUDED.open_price,
          high_price = EXCLUDED.high_price, low_price = EXCLUDED.low_price,
          close_price = EXCLUDED.close_price, vwap = EXCLUDED.vwap,
          ema_20 = EXCLUDED.ema_20, ema_50 = EXCLUDED.ema_50,
          rsi_14 = EXCLUDED.rsi_14, volume = EXCLUDED.volume,
          trade_signals = EXCLUDED.trade_signals, updated_at = EXCLUDED.updated_at
    """)
    for start in range(0, len(rows), 500):
        db.execute(statement, rows[start:start + 500])
        db.commit()


def run(days, symbol):
    kite, _ = fetch_user_token(log)
    token = INSTRUMENTS[symbol]
    end = datetime.now(IST)
    start = end - timedelta(days=days)
    with get_db() as db:
        for label, interval in TIMEFRAMES.items():
            records = kite.historical_data(token, start, end, interval)
            frame = pd.DataFrame(records)
            if frame.empty:
                log.warning("No %s candles returned for %s", label, symbol)
                continue
            frame["date"] = pd.to_datetime(frame["date"])
            rows = snapshot_rows(frame, symbol, label)
            upsert(db, rows)
            log.info("Upserted %s %s candles for %s", len(rows), label, symbol)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=10, choices=range(1, 61))
    parser.add_argument("--symbol", choices=INSTRUMENTS, default="NIFTY 50")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run(args.days, args.symbol)
