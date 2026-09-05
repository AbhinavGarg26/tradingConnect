from datetime import datetime, timedelta
import logging
import json
from zoneinfo import ZoneInfo
from sqlalchemy import text
import pandas as pd

from indicators.calculate_basic_indicator import calculate_basic_indicators
from market.historical_candles import fetch_historical_candles, get_target_candles

logger = logging.getLogger(__name__)

MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")
CANDLE_FINALIZATION_GRACE = timedelta(seconds=5)


def _aggregate_three_hour_candles(df: pd.DataFrame) -> pd.DataFrame:
    """Build NSE-session-aligned 3h candles from Kite's 60-minute candles."""
    if df.empty:
        return df.copy()

    result = df.copy()
    result["date"] = pd.to_datetime(result["date"])
    local_dates = result["date"]
    if local_dates.dt.tz is not None:
        local_dates = local_dates.dt.tz_convert(MARKET_TIMEZONE)

    session_open = local_dates.dt.normalize() + pd.Timedelta(hours=9, minutes=15)
    session_close = local_dates.dt.normalize() + pd.Timedelta(hours=15, minutes=30)
    in_session = (local_dates >= session_open) & (local_dates < session_close)
    result = result.loc[in_session].copy()
    local_dates = local_dates.loc[in_session]
    session_open = session_open.loc[in_session]

    if result.empty:
        return result

    result["_trade_date"] = local_dates.dt.date
    result["_bucket"] = ((local_dates - session_open).dt.total_seconds() // (3 * 60 * 60)).astype(int)

    return (
        result.groupby(["_trade_date", "_bucket"], sort=True, as_index=False)
        .agg({
            "date": "first",
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        })
        .drop(columns=["_trade_date", "_bucket"])
    )


def _completed_candles_only(
    df: pd.DataFrame,
    timeframe: str,
    now: datetime | None = None,
) -> pd.DataFrame:
    """Return candles whose NSE session-adjusted closing time has passed."""
    if df.empty:
        return df.copy()

    durations = {
        "1m": pd.Timedelta(minutes=1),
        "5m": pd.Timedelta(minutes=5),
        "15m": pd.Timedelta(minutes=15),
        "1h": pd.Timedelta(hours=1),
        "3h": pd.Timedelta(hours=3),
    }
    if timeframe not in durations:
        raise ValueError(f"Unsupported database timeframe: {timeframe}")

    result = df.copy()
    result["date"] = pd.to_datetime(result["date"])
    candle_dates = result["date"]
    candle_timezone = candle_dates.dt.tz

    if now is None:
        comparison_now = pd.Timestamp.now(tz=candle_timezone) if candle_timezone else pd.Timestamp.now()
    else:
        comparison_now = pd.Timestamp(now)
        if candle_timezone is not None:
            if comparison_now.tzinfo is None:
                comparison_now = comparison_now.tz_localize(MARKET_TIMEZONE)
            comparison_now = comparison_now.tz_convert(candle_timezone)
        elif comparison_now.tzinfo is not None:
            comparison_now = comparison_now.tz_convert(MARKET_TIMEZONE).tz_localize(None)

    session_close = candle_dates.dt.normalize() + pd.Timedelta(hours=15, minutes=30)
    candle_close = (candle_dates + durations[timeframe]).where(
        candle_dates + durations[timeframe] <= session_close,
        session_close,
    )
    cutoff = comparison_now - CANDLE_FINALIZATION_GRACE
    return result.loc[candle_close <= cutoff].copy()


def sync_timeframe_snapshots(kite, db, symbol, token, interval: str, db_timeframe_label: str):
    """Fetches candles, calculates technical indicators, filters, and inserts missing snapshots."""
    # 1. Fetch deep historical candles (60 days back) for indicator warmup (EMA 50, RSI 14)
    # Intraday replay only needs several sessions, while larger timeframes need
    # deeper history to warm EMA/RSI calculations.
    days_back = 10 if db_timeframe_label in {"1m", "5m"} else 60
    df_raw = fetch_historical_candles(kite, token, interval=interval, days_back=days_back)
    if df_raw.empty:
        logger.warning(f"No candle data returned for interval {interval}.")
        return

    # 2. Aggregate 3h candles on NSE's 09:15 session boundary, not midnight.
    if db_timeframe_label == "3h":
        df = _aggregate_three_hour_candles(df_raw)
    else:
        df = df_raw.copy()

    # Never let an active candle affect stored OHLC values or indicators.
    df = _completed_candles_only(df, db_timeframe_label)
    if df.empty:
        logger.info(f"[{db_timeframe_label}] No completed candles available yet.")
        return

    # 3. Calculate technical indicators (VWAP, EMA 20/50, RSI 14) BEFORE slicing data
    df = calculate_basic_indicators(df)

    # 4. Get target 5 prev-day + today candles
    target_df = get_target_candles(df)
    if target_df.empty:
        return

    # 5. Upsert completed snapshots. This also repairs a candle that may have
    # previously been persisted before it was final.
    upserted_count = 0
    for _, row in target_df.iterrows():
        captured_at = pd.Timestamp(row["date"])
        if captured_at.tzinfo is None:
            captured_at = captured_at.tz_localize(MARKET_TIMEZONE)
        captured_at = captured_at.tz_convert("UTC").to_pydatetime()

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
            "trade_signals": json.dumps({
                "adx_14": clean_val(row.get('adx')),
                "plus_di": clean_val(row.get('plus_di')),
                "minus_di": clean_val(row.get('minus_di')),
                "macd_value": clean_val(row.get('macd_line'), 4),
                "macd_signal": clean_val(row.get('signal_line'), 4),
            }),
            "captured_at": captured_at,
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
                ON CONFLICT (symbol, timeframe, captured_at) DO UPDATE SET
                    ltp = EXCLUDED.ltp,
                    open_price = EXCLUDED.open_price,
                    high_price = EXCLUDED.high_price,
                    low_price = EXCLUDED.low_price,
                    close_price = EXCLUDED.close_price,
                    day_change_pct = EXCLUDED.day_change_pct,
                    vwap = EXCLUDED.vwap,
                    ema_20 = EXCLUDED.ema_20,
                    ema_50 = EXCLUDED.ema_50,
                    rsi_14 = EXCLUDED.rsi_14,
                    trade_signals = EXCLUDED.trade_signals,
                    trend_direction = EXCLUDED.trend_direction,
                    volume = EXCLUDED.volume,
                    updated_at = EXCLUDED.updated_at;
                """),
            snapshot_data
        )
        upserted_count += 1

    db.commit()
    logger.info(f"[{db_timeframe_label}] Upserted {upserted_count} completed candles.")
