"""
indicators/volume.py
====================
Volume analysis for trend confirmation.

Used as a CONVICTION filter — ADX crossovers on thin volume are traps.
Real trend births have participation: volume surges above its recent average.

Exported:
    compute_volume_metrics(df, period=20)  → DataFrame with volume columns
    volume_confirms(row, multiplier=1.3)   → bool
"""

import pandas as pd
import numpy as np


def compute_volume_metrics(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    """
    Add volume analysis columns to DataFrame.

    Output columns:
        vol_sma       : Simple moving average of volume over `period` bars
        vol_ratio     : Current volume / vol_sma  (1.0 = average, 1.5 = 50% above avg)
        vol_expanding : True if current volume > prior bar volume (volume building up)
    """
    df = df.copy()
    df["vol_sma"]       = df["volume"].rolling(period, min_periods=max(1, period // 2)).mean()
    df["vol_ratio"]     = df["volume"] / df["vol_sma"].replace(0, np.nan)
    df["vol_expanding"] = df["volume"] > df["volume"].shift(1)
    return df


def volume_confirms(row: pd.Series, multiplier: float = 1.3) -> bool:
    """
    Returns True if the current bar has sufficient volume conviction.

    Condition: vol_ratio ≥ multiplier
    i.e. current volume is at least 30% above the 20-bar average.

    Why 1.3x?
    - 1.0x = exactly average → no confirmation (could be any random bar)
    - 1.3x = 30% above average → institutional participation is likely
    - 2.0x+ = spike → often reversal, not trend continuation; avoid
    So the window is [1.3, 2.5] — above average but not a panic spike.
    """
    vol_ratio = row.get("vol_ratio", np.nan)
    if pd.isna(vol_ratio):
        return False  # no data → don't block the signal, just skip filter
    return float(vol_ratio) >= multiplier


def volume_not_exhausted(row: pd.Series, spike_cap: float = 2.5) -> bool:
    """
    Returns True if volume is NOT an exhaustion spike.
    Extreme volume (>2.5x avg) on a breakout often marks a climax, not a start.
    """
    vol_ratio = row.get("vol_ratio", np.nan)
    if pd.isna(vol_ratio):
        return True  # benefit of doubt
    return float(vol_ratio) <= spike_cap