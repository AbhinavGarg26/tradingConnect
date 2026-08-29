import pandas as pd
import numpy as np


def calculate_atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    """
    Calculates the Average True Range (ATR).

    True Range = Max of:
    1. Current High - Current Low
    2. Absolute value of (Current High - Previous Close)
    3. Absolute value of (Current Low - Previous Close)
    """
    high = df['high']
    low = df['low']
    prev_close = df['close'].shift(1)

    # Calculate components of True Range
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()

    # Take the max of the three
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    # ATR is the rolling mean of the True Range
    atr = true_range.rolling(window=window).mean()

    return atr