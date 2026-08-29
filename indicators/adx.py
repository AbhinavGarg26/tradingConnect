"""
indicators/adx.py
=================
ADX, +DI, -DI via Wilder smoothing.

Primary signal: DI CROSSOVER while ADX is low (ranging market)
  — +DI crosses above -DI  while ADX < 29  →  BUY
  — -DI crosses above +DI  while ADX < 29  →  SELL

This is different from "ADX rising from below 29".
The crossover of the DI lines is the ACTUAL event. ADX being low
just confirms we're coming out of a range, not chasing a trend.

Real example: Reliance 16 Oct 2024
  ADX ~11-13 (deeply ranged), +DI crossed -DI → price ran 150 points.

Exported:
    compute_adx(df, period=14)
    get_di_crossover_signal(df, i, …)   ← primary signal function
    get_adx_crossover_signal(df, i, …)  ← kept for backward compat
"""

import pandas as pd
import numpy as np


# ─── Computation ─────────────────────────────────────────────────────────────

def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    df    = df.copy()
    high  = df["high"]
    low   = df["low"]
    close = df["close"]

    df["tr"] = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)

    up_move   = high - high.shift(1)
    down_move = low.shift(1) - low

    df["plus_dm"]  = np.where((up_move > down_move)   & (up_move > 0),   up_move,   0.0)
    df["minus_dm"] = np.where((down_move > up_move)   & (down_move > 0), down_move, 0.0)

    df["atr"]      = _wilder(df["tr"],       period)
    plus_di_raw    = _wilder(df["plus_dm"],  period)
    minus_di_raw   = _wilder(df["minus_dm"], period)

    df["plus_di"]  = 100 * plus_di_raw  / df["atr"].replace(0, np.nan)
    df["minus_di"] = 100 * minus_di_raw / df["atr"].replace(0, np.nan)

    di_sum   = (df["plus_di"] + df["minus_di"]).replace(0, np.nan)
    df["dx"] = 100 * (df["plus_di"] - df["minus_di"]).abs() / di_sum
    df["adx"] = _wilder(df["dx"], period)

    return df


def _wilder(series: pd.Series, period: int) -> pd.Series:
    out   = series.copy().astype(float) * np.nan
    valid = series.dropna()
    if len(valid) < period:
        return out

    start = series.index.get_loc(valid.index[0])
    if isinstance(start, slice):
        start = start.start

    seed_end = start + period
    out.iloc[seed_end - 1] = series.iloc[start:seed_end].mean()
    alpha = 1.0 / period
    for i in range(seed_end, len(series)):
        out.iloc[i] = out.iloc[i - 1] * (1.0 - alpha) + series.iloc[i] * alpha
    return out


# ─── PRIMARY SIGNAL: DI Crossover while ADX is low ───────────────────────────

def get_di_crossover_signal(
    df: pd.DataFrame,
    i: int,
    adx_max:          float = 29.0,   # ADX must be BELOW this (ranging condition)
    adx_min:          float = 0.0,    # ADX floor (optional, skip noisy sub-10 readings)
    min_di_gap_after: float = 3.0,    # After crossover, gap must be at least this wide
    min_di_gap_speed: float = 0.0,    # Optional: gap must be widening (gap > prev gap)
    cooldown_bars:    int   = 8,
    last_signal_bar:  int   = -999,
) -> dict:
    """
    Detect the moment +DI crosses -DI (or vice versa) while ADX is in a low range.

    Rules
    -----
    1. ADX[i] < adx_max (29)        → market is ranging, not already trending
    2. ADX[i] >= adx_min            → not pure noise (optional, default 0)
    3. +DI[i] > -DI[i]              → bulls now dominant  (BUY crossover)
       +DI[i-1] <= -DI[i-1]        → bears were dominant last bar
       (reverse for SELL)           → this is the actual crossover bar
    4. |+DI[i] - -DI[i]| >= min_di_gap_after  → gap is meaningful post-cross
    5. Cooldown                     → no signal within last N bars

    The crossover bar is exact: prior bar had bears dominant, this bar bulls take over.
    This catches the 16-Oct-style setup perfectly.
    """
    _none = {"signal": None}

    if i < 2:
        return {**_none, "reason": "insufficient lookback"}

    row  = df.iloc[i]
    prev = df.iloc[i - 1]

    for col in ("adx", "plus_di", "minus_di", "atr"):
        if pd.isna(row[col]) or pd.isna(prev[col]):
            return {**_none, "reason": f"NaN in {col}"}

    adx_now  = float(row["adx"])
    plus_di  = float(row["plus_di"])
    minus_di = float(row["minus_di"])
    prev_plus  = float(prev["plus_di"])
    prev_minus = float(prev["minus_di"])
    di_gap   = plus_di - minus_di
    atr      = float(row["atr"])

    # Rule 1 & 2: ADX must be in the ranging zone
    if adx_now >= adx_max:
        return {**_none, "reason": f"ADX {adx_now:.1f} ≥ {adx_max} (already trending)"}
    if adx_now < adx_min:
        return {**_none, "reason": f"ADX {adx_now:.1f} < {adx_min} (too noisy)"}

    # Rule 3: DI crossover — the exact bar where lines cross
    bull_cross = (plus_di > minus_di) and (prev_plus <= prev_minus)
    bear_cross = (minus_di > plus_di) and (prev_minus <= prev_plus)

    if not bull_cross and not bear_cross:
        return {**_none, "reason": "No DI crossover on this bar"}

    # Rule 4: Gap is meaningful after crossover
    if abs(di_gap) < min_di_gap_after:
        return {**_none, "reason": f"Gap {abs(di_gap):.1f} too small post-crossover"}

    # Rule 5: Cooldown
    if (i - last_signal_bar) < cooldown_bars:
        return {**_none, "reason": f"Cooldown ({i - last_signal_bar} bars since last)"}

    signal = "BUY" if bull_cross else "SELL"
    dom    = f"+DI({plus_di:.1f}) crossed above -DI({minus_di:.1f})" if bull_cross \
             else f"-DI({minus_di:.1f}) crossed above +DI({plus_di:.1f})"
    reason = f"DI crossover while ADX={adx_now:.1f} (ranging); {dom}"

    return {
        "signal":       signal,
        "adx_now":      round(adx_now,   2),
        "plus_di":      round(plus_di,   2),
        "minus_di":     round(minus_di,  2),
        "prev_plus_di": round(prev_plus,  2),
        "prev_minus_di":round(prev_minus, 2),
        "di_gap":       round(di_gap,    2),
        "atr":          round(atr,       2),
        "reason":       reason,
    }


# ─── SECONDARY: ADX momentum crossover (kept for backward compat) ─────────────

def get_adx_crossover_signal(
    df: pd.DataFrame,
    i: int,
    adx_floor:        float = 29.0,
    adx_min_momentum: float = 0.8,
    adx_min_value:    float = 18.0,
    min_di_gap:       float = 8.0,
    cooldown_bars:    int   = 5,
    last_signal_bar:  int   = -999,
) -> dict:
    """Original ADX-rising crossover logic — kept for backward compatibility."""
    _none = {"signal": None}
    if i < 3:
        return {**_none, "reason": "insufficient lookback"}

    row, prev, prev2 = df.iloc[i], df.iloc[i - 1], df.iloc[i - 2]
    for col in ("adx", "plus_di", "minus_di", "atr"):
        if pd.isna(row[col]) or pd.isna(prev[col]):
            return {**_none, "reason": f"NaN in {col}"}

    adx_now   = float(row["adx"])
    adx_prev  = float(prev["adx"])
    adx_prev2 = float(prev2["adx"]) if not pd.isna(prev2["adx"]) else adx_prev
    plus_di   = float(row["plus_di"])
    minus_di  = float(row["minus_di"])
    di_gap    = plus_di - minus_di
    momentum  = adx_now - adx_prev

    if adx_prev  > adx_floor: return {**_none, "reason": f"ADX[i-1]={adx_prev:.1f} above floor"}
    if adx_prev2 > adx_floor: return {**_none, "reason": "Not first bar of crossover"}
    if momentum  < adx_min_momentum: return {**_none, "reason": f"Momentum {momentum:.2f} weak"}
    if adx_now   < adx_min_value:    return {**_none, "reason": f"ADX {adx_now:.1f} below noise floor"}
    if abs(di_gap) < min_di_gap:     return {**_none, "reason": f"|DI gap| {abs(di_gap):.1f} small"}
    if (i - last_signal_bar) < cooldown_bars: return {**_none, "reason": "Cooldown"}

    signal = "BUY" if di_gap > 0 else "SELL"
    return {
        "signal": signal,
        "adx_now": round(adx_now, 2), "adx_prev": round(adx_prev, 2),
        "adx_momentum": round(momentum, 2),
        "plus_di": round(plus_di, 2), "minus_di": round(minus_di, 2),
        "di_gap": round(di_gap, 2), "atr": round(float(row["atr"]), 2),
        "reason": f"ADX {adx_prev:.1f}→{adx_now:.1f} (+{momentum:.2f}) from ≤{adx_floor}",
    }


import pandas as pd
import numpy as np


def calculate_adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """
    Calculates the True Range (TR), Plus Directional Indicator (+DI),
    Minus Directional Indicator (-DI), and Average Directional Index (ADX).
    """
    df = df.copy()

    high = df['high']
    low = df['low']
    close = df['close']

    # Calculate True Range (TR)
    df['tr1'] = high - low
    df['tr2'] = abs(high - close.shift(1))
    df['tr3'] = abs(low - close.shift(1))
    df['tr'] = df[['tr1', 'tr2', 'tr3']].max(axis=1)

    # Calculate Directional Movement (+DM and -DM)
    df['up_move'] = high - high.shift(1)
    df['down_move'] = low.shift(1) - low

    df['+dm'] = np.where((df['up_move'] > df['down_move']) & (df['up_move'] > 0), df['up_move'], 0.0)
    df['-dm'] = np.where((df['down_move'] > df['up_move']) & (df['down_move'] > 0), df['down_move'], 0.0)

    # Wilder's Smoothing Technique
    smooth_tr = np.zeros(len(df))
    smooth_plus_dm = np.zeros(len(df))
    smooth_minus_dm = np.zeros(len(df))

    if len(df) >= period:
        smooth_tr[period - 1] = df['tr'].iloc[0:period].sum()
        smooth_plus_dm[period - 1] = df['+dm'].iloc[0:period].sum()
        smooth_minus_dm[period - 1] = df['-dm'].iloc[0:period].sum()

        for i in range(period, len(df)):
            smooth_tr[i] = smooth_tr[i - 1] - (smooth_tr[i - 1] / period) + df['tr'].iloc[i]
            smooth_plus_dm[i] = smooth_plus_dm[i - 1] - (smooth_plus_dm[i - 1] / period) + df['+dm'].iloc[i]
            smooth_minus_dm[i] = smooth_minus_dm[i - 1] - (smooth_minus_dm[i - 1] / period) + df['-dm'].iloc[i]

    df['smoothed_tr'] = smooth_tr
    df['smoothed_+dm'] = smooth_plus_dm
    df['smoothed_-dm'] = smooth_minus_dm

    # Calculate DI+ and DI-
    df['+DI'] = 100 * (df['smoothed_+dm'] / df['smoothed_tr'])
    df['-DI'] = 100 * (df['smoothed_-dm'] / df['smoothed_tr'])

    # Calculate DX and ADX
    df['dx'] = 100 * (abs(df['+DI'] - df['-DI']) / (df['+DI'] + df['-DI']))
    df['dx'] = df['dx'].fillna(0)

    adx = np.zeros(len(df))
    if len(df) >= (2 * period - 1):
        adx[2 * period - 2] = df['dx'].iloc[period - 1:2 * period - 1].mean()
        for i in range(2 * period - 1, len(df)):
            adx[i] = (adx[i - 1] * (period - 1) + df['dx'].iloc[i]) / period

    df['ADX'] = adx

    # Cleanup temporary columns
    drop_cols = ['tr1', 'tr2', 'tr3', 'tr', 'up_move', 'down_move', '+dm', '-dm', 'smoothed_tr', 'smoothed_+dm',
                 'smoothed_-dm', 'dx']
    return df.drop(columns=drop_cols)