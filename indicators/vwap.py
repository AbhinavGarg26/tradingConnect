import numpy as np
import pandas as pd


def compute_vwap(df: pd.DataFrame) -> pd.DataFrame:
    """Calculates intraday VWAP reset daily at market open (09:15)."""
    df = df.copy()

    # Typical Price = (High + Low + Close) / 3
    tp = (df["high"] + df["low"] + df["close"]) / 3
    pv = tp * df["volume"]

    # Extract date for daily reset
    if "date" in df.columns:
        dates = df["date"].dt.date
    else:
        dates = df.index.date

    # Cumulative sum per day
    cum_pv = pv.groupby(dates).cumsum()
    cum_vol = df["volume"].groupby(dates).cumsum()

    df["vwap"] = cum_pv / cum_vol.replace(0, np.nan)
    return df