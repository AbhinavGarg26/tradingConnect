"""
sr_engine.py
============
Support & Resistance level detection using two independent methods:

  1. Swing Highs / Lows  (daily candles)
     Finds local price peaks and troughs where the market has
     demonstrably reversed.  A "touch" is counted when price
     revisits a prior swing level within a tolerance band.
     Levels with more touches and more recent touches score higher.

  2. Volume Profile  (15-min candles)
     Bins all traded volume into price buckets and identifies
     High Volume Nodes (HVN) — price levels where the most volume
     changed hands.  These act as magnets / S/R because large
     positions were built there.

  3. Confluence merge
     When a swing level and a volume node fall within a proximity
     band (default 0.5%), they are merged into a single level whose
     strength is the sum of both scores.  This produces the levels
     most likely to be respected.

Final output: best 2 supports + 2 resistances relative to the
              reference price (usually today's close).

Usage
-----
    from sr_engine import compute_sr_levels

    levels = compute_sr_levels(
        daily_df   = df_daily,      # pd.DataFrame with OHLCV, sorted asc
        intraday_df= df_15min,      # pd.DataFrame with OHLCV 15-min, sorted asc
        ref_price  = 23547.75,      # current/close price
    )
    # levels keys: support_1, support_2, resistance_1, resistance_2
    # extra keys:  sr_levels_detail  (full list with scores, for debugging)
"""

from __future__ import annotations
import logging
from dataclasses import dataclass, field
from typing      import Optional

import numpy  as np
import pandas as pd

log = logging.getLogger(__name__)

# ── Tuneable parameters ───────────────────────────────────────────────────────

# Swing detection
SWING_WINDOW         = 5      # bars each side to confirm a local high/low
SWING_MIN_TOUCHES    = 1      # min times price revisited level (1 = single swing is ok)
SWING_TOUCH_TOL_PCT  = 0.003  # 0.3% band to count a revisit as a "touch"
SWING_RECENCY_DECAY  = 0.85   # score multiplier per older touch (most-recent = 1.0)

# Volume profile
VP_BUCKET_COUNT      = 120    # number of price buckets across the full range
VP_HVN_THRESHOLD_PCT = 0.70   # top X% of volume = HVN candidate
VP_MIN_HVN_VOL_RATIO = 1.5    # HVN bucket must have >= 1.5× the mean bucket volume

# Confluence
CONFLUENCE_PROXIMITY_PCT = 0.005   # 0.5% — levels closer than this are merged
CONFLUENCE_BONUS         = 1.5     # score multiplier when swing + volume agree


@dataclass
class SRLevel:
    price:    float
    side:     str        # "support" | "resistance"
    method:   str        # "swing" | "volume" | "confluence"
    score:    float      # higher = stronger
    touches:  int = 0
    detail:   dict = field(default_factory=dict)


# ── 1. Swing high / low detection ─────────────────────────────────────────────

def _find_swing_pivots(df: pd.DataFrame, window: int = SWING_WINDOW) -> pd.DataFrame:
    """
    Return rows from df that are confirmed swing highs or lows.
    A swing high: df['high'][i] > max(highs in [i-w, i+w]) excluding i
    A swing low:  df['low'][i]  < min(lows  in [i-w, i+w]) excluding i
    """
    highs = df["high"].values
    lows  = df["low"].values
    n     = len(df)

    swing_high_idx = []
    swing_low_idx  = []

    for i in range(window, n - window):
        left_hi  = highs[i - window : i]
        right_hi = highs[i + 1     : i + window + 1]
        if highs[i] >= max(left_hi) and highs[i] >= max(right_hi):
            swing_high_idx.append(i)

        left_lo  = lows[i - window : i]
        right_lo = lows[i + 1     : i + window + 1]
        if lows[i] <= min(left_lo) and lows[i] <= min(right_lo):
            swing_low_idx.append(i)

    pivots = []
    for i in swing_high_idx:
        pivots.append({"idx": i, "price": highs[i], "kind": "high",
                       "date": df.iloc[i]["date"]})
    for i in swing_low_idx:
        pivots.append({"idx": i, "price": lows[i],  "kind": "low",
                       "date": df.iloc[i]["date"]})

    return pd.DataFrame(pivots) if pivots else pd.DataFrame(
        columns=["idx", "price", "kind", "date"]
    )


def _count_touches(pivot_price: float,
                   highs: np.ndarray,
                   lows:  np.ndarray,
                   tol:   float = SWING_TOUCH_TOL_PCT) -> int:
    """
    Count how many candles touched a price level within ±tol%.
    A candle "touches" if its high >= level*(1-tol) and its low <= level*(1+tol),
    i.e. the level falls within the candle's range.
    """
    band_lo = pivot_price * (1 - tol)
    band_hi = pivot_price * (1 + tol)
    touches = np.sum((highs >= band_lo) & (lows <= band_hi))
    return int(touches)


def compute_swing_sr(daily_df: pd.DataFrame, ref_price: float) -> list[SRLevel]:
    """
    Detect swing highs/lows from daily candles and score them.
    Returns a list of SRLevel objects (unfiltered — caller filters to S vs R).
    """
    if len(daily_df) < SWING_WINDOW * 2 + 1:
        log.warning("Not enough daily candles for swing detection")
        return []

    df     = daily_df.reset_index(drop=True)
    pivots = _find_swing_pivots(df, window=SWING_WINDOW)

    if pivots.empty:
        return []

    highs  = df["high"].values
    lows   = df["low"].values
    n      = len(df)

    levels: list[SRLevel] = []
    now    = df["date"].max()

    for _, piv in pivots.iterrows():
        price  = float(piv["price"])
        kind   = piv["kind"]        # "high" → resistance candidate, "low" → support candidate
        p_date = piv["date"]

        touches = _count_touches(price, highs, lows)
        if touches < SWING_MIN_TOUCHES:
            continue

        # Recency score: most-recent touch = 1.0, decay per bar back
        age_bars = int((now - p_date).days / 1)   # approx — daily
        recency  = SWING_RECENCY_DECAY ** min(age_bars, 60)

        score = touches * recency

        side = "resistance" if kind == "high" else "support"

        levels.append(SRLevel(
            price   = round(price, 2),
            side    = side,
            method  = "swing",
            score   = round(score, 3),
            touches = touches,
            detail  = {"age_bars": age_bars, "recency": round(recency, 3)},
        ))

    return levels


# ── 2. Volume Profile ─────────────────────────────────────────────────────────

def compute_volume_profile_sr(intraday_df: pd.DataFrame, ref_price: float) -> list[SRLevel]:
    """
    Build a volume profile from 15-min candles and identify High Volume Nodes.
    HVNs below ref_price → support; HVNs above → resistance.
    """
    if intraday_df.empty or "volume" not in intraday_df.columns:
        log.warning("No intraday data for volume profile")
        return []

    df = intraday_df.copy()

    price_min = df["low"].min()
    price_max = df["high"].max()
    if price_max <= price_min:
        return []

    bucket_size = (price_max - price_min) / VP_BUCKET_COUNT
    if bucket_size == 0:
        return []

    # Distribute each candle's volume across the price buckets it spans
    # (typical price approximation: we assign volume to the candle's mid-price bucket)
    df["mid"] = (df["high"] + df["low"]) / 2
    df["bucket"] = ((df["mid"] - price_min) / bucket_size).astype(int).clip(0, VP_BUCKET_COUNT - 1)

    profile = df.groupby("bucket")["volume"].sum()

    # Fill missing buckets with 0
    full_profile = profile.reindex(range(VP_BUCKET_COUNT), fill_value=0)
    vol_mean = full_profile[full_profile > 0].mean()
    if vol_mean == 0 or np.isnan(vol_mean):
        return []

    # HVN threshold: top VP_HVN_THRESHOLD_PCT percentile AND >= 1.5× mean
    vol_threshold = max(
        full_profile.quantile(VP_HVN_THRESHOLD_PCT),
        vol_mean * VP_MIN_HVN_VOL_RATIO
    )

    hvn_buckets = full_profile[full_profile >= vol_threshold]

    levels: list[SRLevel] = []

    for bucket_idx, vol in hvn_buckets.items():
        # Centre price of this bucket
        bucket_price = price_min + (bucket_idx + 0.5) * bucket_size
        score        = vol / vol_mean   # ratio to mean — e.g. 3.2 = 3.2× average

        side = "support" if bucket_price < ref_price else "resistance"

        levels.append(SRLevel(
            price   = round(bucket_price, 2),
            side    = side,
            method  = "volume",
            score   = round(float(score), 3),
            touches = 0,
            detail  = {
                "volume":     int(vol),
                "vol_mean":   int(vol_mean),
                "vol_ratio":  round(float(score), 2),
            },
        ))

    return levels


# ── 3. Merge + rank ───────────────────────────────────────────────────────────

def _cluster_levels(levels: list[SRLevel],
                    proximity_pct: float = CONFLUENCE_PROXIMITY_PCT) -> list[SRLevel]:
    """
    Merge levels within proximity_pct of each other (by side).
    Merged level: price = volume-weighted average, score = sum × confluence bonus
    if both methods contributed, else sum.
    """
    if not levels:
        return []

    clustered: list[SRLevel] = []
    used = [False] * len(levels)

    # Sort by price for easy proximity scanning
    sorted_levels = sorted(levels, key=lambda l: l.price)

    for i, lv in enumerate(sorted_levels):
        if used[i]:
            continue

        group = [lv]
        used[i] = True

        for j in range(i + 1, len(sorted_levels)):
            if used[j]:
                continue
            other = sorted_levels[j]
            if other.side != lv.side:
                continue
            if abs(other.price - lv.price) / lv.price <= proximity_pct:
                group.append(other)
                used[j] = True

        if len(group) == 1:
            clustered.append(lv)
            continue

        # Merge group
        methods  = {g.method for g in group}
        total_score = sum(g.score for g in group)
        # Apply confluence bonus if both swing and volume agreed
        if "swing" in methods and "volume" in methods:
            total_score *= CONFLUENCE_BONUS
            merged_method = "confluence"
        else:
            merged_method = list(methods)[0]

        # Price = score-weighted average
        weights     = [g.score for g in group]
        total_w     = sum(weights) or 1
        merged_price = sum(g.price * w for g, w in zip(group, weights)) / total_w
        merged_touches = max(g.touches for g in group)

        clustered.append(SRLevel(
            price   = round(merged_price, 2),
            side    = lv.side,
            method  = merged_method,
            score   = round(total_score, 3),
            touches = merged_touches,
            detail  = {"merged_count": len(group), "methods": list(methods)},
        ))

    return clustered


def compute_sr_levels(
    daily_df:    pd.DataFrame,
    intraday_df: pd.DataFrame,
    ref_price:   float,
    n_levels:    int = 2,          # how many S and R levels to return
) -> dict:
    """
    Main entry point.  Returns dict with keys:
      support_1, support_2, resistance_1, resistance_2
      sr_levels_detail  (full ranked list, for logging/debugging)

    daily_df    : sorted ascending, columns [date, open, high, low, close, volume]
    intraday_df : sorted ascending, same schema but 15-min granularity
    ref_price   : reference price to classify S vs R (usually today's close)
    """
    # Compute raw levels from each method
    swing_levels  = compute_swing_sr(daily_df, ref_price)
    volume_levels = compute_volume_profile_sr(intraday_df, ref_price)

    all_levels = swing_levels + volume_levels

    if not all_levels:
        log.warning("No S/R levels detected — returning None values")
        return {
            "support_1": None, "support_2": None,
            "resistance_1": None, "resistance_2": None,
            "sr_levels_detail": [],
        }

    # Cluster nearby levels, applying confluence bonus where methods agree
    merged = _cluster_levels(all_levels)

    # Separate into supports and resistances, sort by proximity to ref_price
    supports    = sorted(
        [l for l in merged if l.side == "support" and l.price < ref_price],
        key=lambda l: (ref_price - l.price)   # closest first
    )
    resistances = sorted(
        [l for l in merged if l.side == "resistance" and l.price > ref_price],
        key=lambda l: (l.price - ref_price)   # closest first
    )

    # Among same-distance candidates, prefer higher-score levels
    # Re-sort: primary = proximity bucket (within 1% steps), secondary = score desc
    def proximity_rank(level: SRLevel) -> tuple:
        dist_pct = abs(level.price - ref_price) / ref_price
        bucket   = int(dist_pct / 0.01)        # 1% buckets
        return (bucket, -level.score)

    supports    = sorted(supports,    key=proximity_rank)
    resistances = sorted(resistances, key=proximity_rank)

    def _price(lst, i) -> Optional[float]:
        return lst[i].price if i < len(lst) else None

    result = {
        "support_1":    _price(supports,    0),
        "support_2":    _price(supports,    1),
        "resistance_1": _price(resistances, 0),
        "resistance_2": _price(resistances, 1),
        "sr_levels_detail": [
            {
                "price":   l.price,
                "side":    l.side,
                "method":  l.method,
                "score":   l.score,
                "touches": l.touches,
            }
            for l in sorted(merged, key=lambda x: x.price)
        ],
    }

    log.debug(
        f"  S/R computed @ {ref_price}: "
        f"S1={result['support_1']} S2={result['support_2']} "
        f"R1={result['resistance_1']} R2={result['resistance_2']}"
    )
    return result