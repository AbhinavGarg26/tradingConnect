from datetime import datetime
import logging
from sqlalchemy.orm import Session
from sqlalchemy import text, bindparam
import pandas as pd

from indicators.calculate_basic_indicator import calculate_basic_indicators
from market.historical_candles import fetch_historical_candles, get_target_candles

logger = logging.getLogger(__name__)


def get_existing_timestamps(symbol, db: Session, timeframe: str, timestamps: list) -> set:
    """Queries database to return timestamps that are already persisted."""
    if not timestamps:
        return set()

    formatted_ts = tuple(ts.strftime("%Y-%m-%d %H:%M:%S") for ts in timestamps)

    # Adjust SQL syntax based on ORM/driver (SQLAlchemy / psycopg2 / etc.)
    query = text("""
        SELECT captured_at 
        FROM market_snapshots 
        WHERE symbol = :symbol 
          AND timeframe = :timeframe 
          AND captured_at IN :timestamps
    """).bindparams(bindparam("timestamps", expanding=True))

    result = db.execute(
        query,
        {
            "symbol": symbol,
            "timeframe": timeframe,
            "timestamps": [ts.strftime("%Y-%m-%d %H:%M:%S") for ts in timestamps]  # List instead of tuple
        }
    ).fetchall()

    return {row[0].strftime("%Y-%m-%d %H:%M:%S") if isinstance(row[0], datetime) else str(row[0]) for row in result}


def sync_timeframe_snapshots(kite, db, symbol, token, interval: str, db_timeframe_label: str):
    """Fetches candles, calculates technical indicators, filters, and inserts missing snapshots."""
    # 1. Fetch deep historical candles (60 days back) for indicator warmup (EMA 50, RSI 14)
    df_raw = fetch_historical_candles(kite, token, interval=interval, days_back=60)
    if df_raw.empty:
        logger.warning(f"No candle data returned for interval {interval}.")
        return

    # 2. Resample if 3h timeframe
    if db_timeframe_label == "3h":
        df_raw.set_index('date', inplace=True)
        df_resampled = df_raw.resample('3h').agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum'
        }).dropna().reset_index()
        df = df_resampled
    else:
        df = df_raw

    # 3. Calculate technical indicators (VWAP, EMA 20/50, RSI 14) BEFORE slicing data
    df = calculate_basic_indicators(df)

    # 4. Get target 5 prev-day + today candles
    target_df = get_target_candles(df)
    if target_df.empty:
        return

    # 5. Check existing records in DB
    target_timestamps = target_df['date'].tolist()
    saved_timestamps = get_existing_timestamps(symbol, db, db_timeframe_label, target_timestamps)

    # 6. Insert missing snapshots with calculated values
    inserted_count = 0
    for _, row in target_df.iterrows():
        ts_str = row['date'].strftime("%Y-%m-%d %H:%M:%S")
        if ts_str in saved_timestamps:
            continue

        day_change = round(((row['close'] - row['open']) / row['open']) * 100, 2)
        trend = "bullish" if row['close'] >= row['open'] else "bearish"

        # Helper to handle NaN/None values safely for database insertion
        def clean_val(val, round_places=2):
            if pd.isna(val) or val is None:
                return None
            return round(float(val), round_places)

        snapshot_data = {
            "symbol": symbol,
            "timeframe": db_timeframe_label,
            "ltp": float(row['close']),
            "open_price": float(row['open']),
            "high_price": float(row['high']),
            "low_price": float(row['low']),
            "close_price": float(row['close']),
            "day_change_pct": day_change,
            "week_change_pct": 0.0,
            "month_change_pct": 0.0,
            "support_1": None,
            "support_2": None,
            "resistance_1": None,
            "resistance_2": None,

            # --- POPULATED INDICATOR VALUES ---
            "vwap": clean_val(row.get('vwap')),
            "ema_20": clean_val(row.get('ema_20')),
            "ema_50": clean_val(row.get('ema_50')),
            "rsi_14": clean_val(row.get('rsi')),

            "trend_direction": trend,
            "mood_label": "Neutral",
            "mood_score": 0,
            "volume": int(row.get('volume', 0)),
            "avg_volume_20d": 0,
            "trade_signals": "[]",
            "captured_at": ts_str,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

        db.execute(
            text("""
                INSERT INTO market_snapshots (
                    symbol, timeframe, ltp, open_price, high_price, low_price, 
                    close_price, day_change_pct, week_change_pct, month_change_pct, support_1, 
                    support_2, resistance_1, resistance_2, vwap, ema_20, ema_50, rsi_14, 
                    trend_direction, mood_label, mood_score, volume, avg_volume_20d, 
                    trade_signals, captured_at, created_at, updated_at
                ) VALUES (
                    :symbol, :timeframe, :ltp, :open_price, :high_price, :low_price,
                    :close_price, :day_change_pct, :week_change_pct, :month_change_pct, :support_1,
                    :support_2, :resistance_1, :resistance_2, :vwap, :ema_20, :ema_50, :rsi_14,
                    :trend_direction, :mood_label, :mood_score, :volume, :avg_volume_20d,
                    :trade_signals, :captured_at, :created_at, :updated_at
                )
                ON CONFLICT (symbol, timeframe, captured_at) DO NOTHING;
                """),
            snapshot_data
        )
        inserted_count += 1

    db.commit()
    logger.info(f"[{db_timeframe_label}] Processed {len(target_df)} candles. Inserted {inserted_count} new records.")
