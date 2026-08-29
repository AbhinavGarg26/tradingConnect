from indicators import compute_ema, compute_rsi
from indicators.vwap import compute_vwap
import pandas as pd


def calculate_basic_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Applies VWAP, EMA 20/50, and RSI 14 to the full historical DataFrame."""
    if df.empty or len(df) < 2:
        return df

    # Calculate VWAP
    df = compute_vwap(df)

    # Calculate EMAs (passing 20 and 50 explicitly)
    df = compute_ema(df, periods=[20, 50], column="close")

    # Calculate RSI
    df = compute_rsi(df, period=14, column="close")

    return df