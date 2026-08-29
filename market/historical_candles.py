from datetime import datetime, timedelta
import logging
import pandas as pd


logger = logging.getLogger(__name__)


def fetch_historical_candles(kite, instrument_token: int, interval: str, days_back: int = 5) -> pd.DataFrame:
    """Fetches OHLC data from Kite for a given timeframe interval."""
    to_date = datetime.now()
    from_date = to_date - timedelta(days=days_back)
    try:
        records = kite.historical_data(
            instrument_token=instrument_token,
            from_date=from_date.strftime("%Y-%m-%d %H:%M:%S"),
            to_date=to_date.strftime("%Y-%m-%d %H:%M:%S"),
            interval=interval
        )
        df = pd.DataFrame(records)
        if not df.empty:
            df['date'] = pd.to_datetime(df['date'])
        return df
    except Exception as e:
        logger.error(f"Failed to fetch historical data for {interval}: {e}")
        return pd.DataFrame()


def get_target_candles(df: pd.DataFrame) -> pd.DataFrame:
    """
    Extracts the last 5 candles of the previous trading day
    plus all candles generated on the current trading day.
    """
    if df.empty:
        return pd.DataFrame()

    df['trade_date'] = df['date'].dt.date
    unique_dates = sorted(df['trade_date'].unique())

    if len(unique_dates) < 2:
        return df

    prev_day = unique_dates[-2]
    curr_day = unique_dates[-1]

    prev_day_candles = df[df['trade_date'] == prev_day].tail(5)
    curr_day_candles = df[df['trade_date'] == curr_day]

    return pd.concat([prev_day_candles, curr_day_candles])