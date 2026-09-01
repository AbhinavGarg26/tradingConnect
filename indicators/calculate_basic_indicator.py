from indicators import compute_ema, compute_rsi
from indicators.vwap import compute_vwap
from indicators.adx import compute_adx
from indicators.macd import calculate_macd
import pandas as pd


def calculate_basic_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Applies the tracker indicators to the full historical DataFrame."""
    if df.empty or len(df) < 2:
        return df

    # Calculate VWAP
    df = compute_vwap(df)

    # Calculate EMAs (passing 20 and 50 explicitly)
    df = compute_ema(df, periods=[20, 50], column="close")

    # Calculate RSI
    df = compute_rsi(df, period=14, column="close")

    # Keep these in the same pass so every saved snapshot represents one
    # internally consistent candle and indicator timestamp.
    df = calculate_macd(df)
    df = compute_adx(df, period=14)

    return df
