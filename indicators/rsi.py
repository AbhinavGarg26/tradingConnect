"""
indicators/rsi.py
=================
RSI via Wilder smoothing (same method as original Welles Wilder formula).
Used for DIRECTION CONFIRMATION only — not overbought/oversold.

Logic in this strategy:
    BUY  signal: RSI > 55  (momentum is bullish, not just bouncing from oversold)
    SELL signal: RSI < 45  (momentum is bearish, not just pulling back from overbought)

The 55/45 zone is intentional — it filters out dead-cat bounces and
bear-market rallies that fool a DI crossover without real momentum.

Exported:
    compute_rsi(df, period=14)     → DataFrame with 'rsi' column
    rsi_confirms(rsi_val, signal)  → bool
"""

import pandas as pd
import numpy as np


def compute_rsi(df: pd.DataFrame, period: int = 14, column: str = "close") -> pd.DataFrame:
    """
    Add RSI column to DataFrame using Wilder's smoothing method.
    Output column: rsi
    """
    df    = df.copy()
    delta = df[column].diff()

    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)

    # Wilder smoothing for avg gain/loss (same alpha = 1/period)
    avg_gain = _wilder_rsi(gain, period)
    avg_loss = _wilder_rsi(loss, period)

    rs         = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"]  = 100 - (100 / (1 + rs))

    return df


def _wilder_rsi(series: pd.Series, period: int) -> pd.Series:
    """Wilder smoothing seeded with SMA of first `period` valid values."""
    out   = series.copy().astype(float) * np.nan
    valid = series.dropna()

    if len(valid) < period:
        return out

    start = series.index.get_loc(valid.index[0])
    if isinstance(start, slice):
        start = start.start

    seed_end            = start + period
    out.iloc[seed_end - 1] = series.iloc[start:seed_end].mean()

    alpha = 1.0 / period
    for i in range(seed_end, len(series)):
        out.iloc[i] = out.iloc[i - 1] * (1.0 - alpha) + series.iloc[i] * alpha

    return out


def rsi_confirms(rsi_val: float, signal: str, bull_threshold: float = 55.0, bear_threshold: float = 45.0) -> bool:
    """
    Returns True if RSI confirms the trade direction.

    BUY  → RSI must be above bull_threshold (55): price has real upside momentum
    SELL → RSI must be below bear_threshold (45): price has real downside momentum

    Why not standard 70/30?
    Because 70/30 is for reversal trades. We're trend-following.
    RSI 55+ means bulls are in control, not just recovering from oversold.
    """
    if pd.isna(rsi_val):
        return False
    if signal == "BUY":
        return rsi_val > bull_threshold
    if signal == "SELL":
        return rsi_val < bear_threshold
    return False