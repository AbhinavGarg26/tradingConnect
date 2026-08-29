"""
indicators/ema.py
=================
Multi-period EMA calculation.

Used as a TREND BIAS filter:
    BUY  signals only when price > EMA50 (bullish macro structure)
    SELL signals only when price < EMA50 (bearish macro structure)

EMA200 is computed for long-term context display in reports.

Exported:
    compute_ema(df, periods=[21, 50, 200])  → DataFrame with ema_N columns
"""

import pandas as pd


def compute_ema(df: pd.DataFrame, periods: list = None, column: str = "close") -> pd.DataFrame:
    """
    Add EMA columns. Column names: ema_21, ema_50, ema_200, etc.
    Uses pandas ewm with adjust=False (standard EMA, not Wilder).
    """
    if periods is None:
        periods = [21, 50, 200]

    df = df.copy()
    for p in periods:
        df[f"ema_{p}"] = df[column].ewm(span=p, adjust=False).mean()

    return df


def price_above_ema(row: pd.Series, period: int = 50) -> bool:
    """True if close is above the given EMA period."""
    val = row.get(f"ema_{period}")
    if val is None:
        return True  # filter disabled if column missing
    return float(row["close"]) > float(val)


def price_below_ema(row: pd.Series, period: int = 50) -> bool:
    """True if close is below the given EMA period."""
    val = row.get(f"ema_{period}")
    if val is None:
        return True
    return float(row["close"]) < float(val)