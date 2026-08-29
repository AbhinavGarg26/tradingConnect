"""
strategies/adx_crossover.py  v4
================================
DI Crossover while ADX is ranging — tightened entry filters

Changes from v3 (based on real trade analysis):
  • ADX ceiling: 29 → 25  (ADX=28.5 Aug trade was a failed late-range entry)
  • RSI window for BUY:  45–58  (was >52 — blocks RSI=63.8 overextended entries)
  • RSI window for SELL: 42–55  (was <48 — blocks RSI=38.3 oversold entries)
  • Both are now a BAND not a floor/ceiling: momentum must be directional
    but NOT already extended. This is the key insight — at DI crossover from
    a ranging market, RSI should be in the middle zone, starting to lean.
    If RSI is already at 63 or 38, the move is half-done.

Everything else unchanged:
  • Signal: +DI crosses -DI while ADX < 25
  • Entry: next bar open
  • Volume: 1.2x–3.0x 20-bar avg
  • EMA filter: off by default (price near EMAs in ranging markets)
  • Cooldown: 8 bars
"""

import pandas as pd
import numpy as np

from indicators.adx import compute_adx, get_di_crossover_signal, calculate_adx
from indicators.atr import calculate_atr
from indicators.macd import calculate_macd
from indicators.rsi    import compute_rsi
from indicators.volume import compute_volume_metrics, volume_confirms, volume_not_exhausted
from indicators.ema    import compute_ema, price_above_ema, price_below_ema


def rsi_in_band(rsi_val: float, signal: str,
                buy_lo=45.0, buy_hi=58.0,
                sell_lo=42.0, sell_hi=55.0) -> bool:
    """
    RSI must be in the directional-but-not-extended band.
    BUY:  45–58  (leaning bullish, not overbought)
    SELL: 42–55  (leaning bearish, not oversold)
    Outside the band = move already happened, don't chase.
    """
    if pd.isna(rsi_val):
        return False
    if signal == "BUY":
        return buy_lo <= rsi_val <= buy_hi
    if signal == "SELL":
        return sell_lo <= rsi_val <= sell_hi
    return False


def apply_advanced_strategy(df: pd.DataFrame, atr_multiplier=2.5) -> pd.DataFrame:
    # 1. Initialize 'signal' to 0 immediately so it always exists
    df['signal'] = 0

    # Check if we have enough data to calculate indicators
    if len(df) < 200:
        return df

    # 2. Calculate Indicators
    df = calculate_adx(df)
    df = calculate_macd(df)
    df['SMA_200'] = df['close'].rolling(window=200).mean()
    df['ATR'] = calculate_atr(df, window=14)

    # Drop NaNs early to prevent calculation errors
    df.dropna(inplace=True)

    # 3. Logic Gates
    is_bullish_regime = df['close'] > df['SMA_200']
    is_bearish_regime = df['close'] < df['SMA_200']
    is_trending = (df['ADX'] > 25) & (df['ADX'] > df['ADX'].shift(1))

    # 4. Signal Generation
    # We use .loc to safely update the 'signal' column
    long_mask = (df['+DI'] > df['-DI']) & is_trending & (df['macd_line'] > df['signal_line']) & is_bullish_regime
    short_mask = (df['-DI'] > df['+DI']) & is_trending & (df['macd_line'] < df['signal_line']) & is_bearish_regime

    df.loc[long_mask, 'signal'] = 1
    df.loc[short_mask, 'signal'] = -1

    return df


def apply_adx_crossover_strategy(df: pd.DataFrame, adx_threshold: float = 20.0) -> pd.DataFrame:
    """
    Applies ADX breakout strategy rules.
    ADX threshold lowered to 20.0 to capture earlier momentum shifts.
    """
    atr_multiplier = 2.5
    df = calculate_adx(df)
    df['signal'] = 0
    df['long_stop'] = np.nan
    df['short_stop'] = np.nan

    df['prev_+DI'] = df['+DI'].shift(1)
    df['prev_-DI'] = df['-DI'].shift(1)
    df['prev_ADX'] = df['ADX'].shift(1)

    # Price Action Filter
    df['is_green_candle'] = df['close'] > df['open']
    df['is_red_candle'] = df['close'] < df['open']

    df['ATR'] = calculate_atr(df, window=14)

    # Calculate Dynamic Trailing Stops
    df['long_stop'] = df['high'].rolling(window=10).max() - (df['ATR'] * atr_multiplier)
    df['short_stop'] = df['low'].rolling(window=10).min() + (df['ATR'] * atr_multiplier)
    df.dropna(subset=['long_stop', 'short_stop'], inplace=True)

    # Long Conditions
    di_long_cross = (df['prev_+DI'] <= df['prev_-DI']) & (df['+DI'] > df['-DI'])
    di_long_upward = df['+DI'] > df['prev_+DI']
    adx_waking_up = (df['prev_ADX'] <= adx_threshold) & (df['ADX'] > df['prev_ADX'])

    # Short Conditions
    di_short_cross = (df['prev_-DI'] <= df['prev_+DI']) & (df['-DI'] > df['+DI'])
    di_short_upward = df['-DI'] > df['prev_-DI']

    df.loc[di_long_cross & di_long_upward & adx_waking_up & df['is_green_candle'], 'signal'] = 1
    df.loc[di_short_cross & di_short_upward & adx_waking_up & df['is_red_candle'], 'signal'] = -1

    return df

class ADXCrossoverStrategy:
    """
    Parameters
    ----------
    adx_max           : ADX must be BELOW this (default 25, tightened from 29)
    adx_min           : ADX floor to avoid pure noise (default 8)
    min_di_gap        : |+DI−-DI| minimum after crossover (default 3)
    rsi_buy_lo/hi     : RSI window for BUY  (default 45–58)
    rsi_sell_lo/hi    : RSI window for SELL (default 42–55)
    vol_min_mult      : Volume min vs 20-bar avg (default 1.2)
    vol_max_mult      : Volume spike cap (default 3.0)
    atr_sl_mult       : ATR mult for initial SL (default 1.5)
    rr_target         : R:R for fixed target (default 3.0)
    cooldown_bars     : Bars between signals (default 8)
    rsi_filter        : Enable RSI band filter (default True)
    volume_filter     : Enable volume filter (default True)
    ema_filter        : Enable EMA50 bias (default False)
    ema_filter_period : EMA period if enabled (default 50)
    """

    def __init__(
        self,
        adx_max:           float = 25.0,
        adx_min:           float = 8.0,
        min_di_gap:        float = 3.0,
        rsi_buy_lo:        float = 45.0,
        rsi_buy_hi:        float = 58.0,
        rsi_sell_lo:       float = 42.0,
        rsi_sell_hi:       float = 55.0,
        vol_min_mult:      float = 1.2,
        vol_max_mult:      float = 3.0,
        atr_sl_mult:       float = 1.5,
        rr_target:         float = 3.0,
        cooldown_bars:     int   = 8,
        rsi_filter:        bool  = True,
        volume_filter:     bool  = True,
        ema_filter:        bool  = False,
        ema_filter_period: int   = 50,
    ):
        self.adx_max           = adx_max
        self.adx_min           = adx_min
        self.min_di_gap        = min_di_gap
        self.rsi_buy_lo        = rsi_buy_lo
        self.rsi_buy_hi        = rsi_buy_hi
        self.rsi_sell_lo       = rsi_sell_lo
        self.rsi_sell_hi       = rsi_sell_hi
        self.vol_min_mult      = vol_min_mult
        self.vol_max_mult      = vol_max_mult
        self.atr_sl_mult       = atr_sl_mult
        self.rr_target         = rr_target
        self.cooldown_bars     = cooldown_bars
        self.rsi_filter        = rsi_filter
        self.volume_filter     = volume_filter
        self.ema_filter        = ema_filter
        self.ema_filter_period = ema_filter_period

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df.columns = [c.lower() for c in df.columns]
        df = compute_adx(df, period=14)
        df = compute_rsi(df, period=14)
        df = compute_volume_metrics(df, period=20)
        df = compute_ema(df, periods=[21, self.ema_filter_period, 200])
        return df

    def run(self, df: pd.DataFrame) -> list[dict]:
        df              = self.prepare(df)
        signals         = []
        last_signal_bar = -999

        for i in range(2, len(df) - 1):
            row = df.iloc[i]

            sig = get_di_crossover_signal(
                df, i,
                adx_max          = self.adx_max,
                adx_min          = self.adx_min,
                min_di_gap_after = self.min_di_gap,
                cooldown_bars    = self.cooldown_bars,
                last_signal_bar  = last_signal_bar,
            )
            if sig["signal"] is None:
                continue

            direction      = sig["signal"]
            filters_passed = []

            # ── RSI band filter ───────────────────────────────────────────
            if self.rsi_filter:
                rsi_val = float(row.get("rsi", np.nan))
                if not rsi_in_band(rsi_val, direction,
                                   self.rsi_buy_lo, self.rsi_buy_hi,
                                   self.rsi_sell_lo, self.rsi_sell_hi):
                    continue
                filters_passed.append(f"RSI={rsi_val:.1f}✓")

            # ── Volume filter ─────────────────────────────────────────────
            if self.volume_filter:
                if not volume_confirms(row, self.vol_min_mult):
                    continue
                if not volume_not_exhausted(row, self.vol_max_mult):
                    continue
                vr = row.get("vol_ratio", np.nan)
                filters_passed.append(f"vol={vr:.2f}x✓")

            # ── EMA bias (optional) ───────────────────────────────────────
            if self.ema_filter:
                if direction == "BUY"  and not price_above_ema(row, self.ema_filter_period):
                    continue
                if direction == "SELL" and not price_below_ema(row, self.ema_filter_period):
                    continue
                filters_passed.append(f"EMA{self.ema_filter_period}✓")

            # ── Entry: next bar open ──────────────────────────────────────
            next_row   = df.iloc[i + 1]
            entry      = round(float(next_row["open"]), 2)
            entry_date = df.index[i + 1]
            atr        = sig["atr"]
            if pd.isna(atr) or atr == 0:
                continue

            sl_dist = round(atr * self.atr_sl_mult, 2)
            if direction == "BUY":
                sl     = round(entry - sl_dist, 2)
                target = round(entry + sl_dist * self.rr_target, 2)
            else:
                sl     = round(entry + sl_dist, 2)
                target = round(entry - sl_dist * self.rr_target, 2)

            last_signal_bar = i
            rsi_val = float(row.get("rsi", np.nan))
            vol_val = float(row.get("vol_ratio", np.nan))

            signals.append({
                "signal_date":   df.index[i],
                "date":          entry_date,
                "signal":        direction,
                "entry":         entry,
                "sl":            sl,
                "target":        target,
                "risk":          sl_dist,
                "adx":           sig["adx_now"],
                "plus_di":       sig["plus_di"],
                "minus_di":      sig["minus_di"],
                "prev_plus_di":  sig["prev_plus_di"],
                "prev_minus_di": sig["prev_minus_di"],
                "di_gap":        sig["di_gap"],
                "rsi":           round(rsi_val, 1) if not pd.isna(rsi_val) else None,
                "vol_ratio":     round(vol_val, 2)  if not pd.isna(vol_val) else None,
                "atr":           round(atr, 2),
                "confirmations": " | ".join(filters_passed),
                "reason":        sig["reason"],
            })

        return signals

    def summary(self, signals):
        buys  = [s for s in signals if s["signal"] == "BUY"]
        sells = [s for s in signals if s["signal"] == "SELL"]
        return {"total": len(signals), "buys": len(buys), "sells": len(sells), "signals": signals}