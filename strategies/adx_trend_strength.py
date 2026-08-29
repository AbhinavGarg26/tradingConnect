"""
Strategy: ADX Trend Strength Continuation
==========================================
This is a COMPLEMENTARY strategy to adx_crossover.py.

Signal logic:
  - ADX > 25 and RISING (trend already established)
  - +DI is well above -DI (spread > 8) for BUY
  - Price pulls back to EMA21 but doesn't break it (trend continuation)
  - ADX hasn't peaked (current ADX > ADX 2 bars ago)

This catches the SECOND LEG of a strong trend — after the ADX
crossover setup from adx_crossover.py triggers, this strategy
catches re-entries on pullbacks within the same trend.

Combine with adx_crossover.py in a two-signal system:
  - Crossover = first entry (trend inception)
  - TrendStrength = re-entry on pullbacks (trend continuation)

Entry  : Close of signal candle
SL     : Below EMA21 (for BUY) + 0.5x ATR buffer
Target : 2.5:1 R:R (higher because trend is confirmed)

Usage:
    from strategies.adx_trend_strength import ADXTrendStrengthStrategy
    strategy = ADXTrendStrengthStrategy()
    signals  = strategy.run(df)
"""

import pandas as pd
import numpy as np

from indicators.adx import compute_adx
from indicators.ema import compute_ema


class ADXTrendStrengthStrategy:
    """
    ADX Trend Continuation / Pullback Re-entry strategy.

    Parameters
    ----------
    adx_min         : ADX must be above this (trend confirmed, default 25)
    di_spread_min   : Minimum DI spread required (default 8)
    ema_pullback    : EMA period for pullback zone (default 21)
    atr_sl_buffer   : Buffer beyond EMA for SL in ATR units (default 0.5)
    rr_target       : Reward:Risk ratio (default 2.5)
    adx_rising_bars : ADX must be rising over this many bars (default 2)
    """

    def __init__(
        self,
        adx_min: float = 25.0,
        di_spread_min: float = 8.0,
        ema_pullback: int = 21,
        atr_sl_buffer: float = 0.5,
        rr_target: float = 2.5,
        adx_rising_bars: int = 2,
    ):
        self.adx_min        = adx_min
        self.di_spread_min  = di_spread_min
        self.ema_pullback   = ema_pullback
        self.atr_sl_buffer  = atr_sl_buffer
        self.rr_target      = rr_target
        self.adx_rising_bars = adx_rising_bars

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df.columns = [c.lower() for c in df.columns]
        df = compute_adx(df, period=14)
        df = compute_ema(df, periods=[9, 21, 50])
        return df

    def run(self, df: pd.DataFrame) -> list[dict]:
        df = self.prepare(df)
        signals = []
        lookback = self.adx_rising_bars

        for i in range(lookback + 1, len(df)):
            row      = df.iloc[i]
            prev_row = df.iloc[i - 1]
            old_row  = df.iloc[i - lookback]

            if pd.isna(row["adx"]) or pd.isna(old_row["adx"]):
                continue

            adx_now  = row["adx"]
            adx_old  = old_row["adx"]
            plus_di  = row["plus_di"]
            minus_di = row["minus_di"]
            close    = row["close"]
            atr      = row.get("atr", np.nan)
            ema_pb   = row.get(f"ema_{self.ema_pullback}", np.nan)

            if pd.isna(atr) or pd.isna(ema_pb):
                continue

            # ADX confirmed trend and still rising
            adx_trending = adx_now >= self.adx_min and adx_now > adx_old

            # Price near EMA (pullback zone = within 1.5x ATR)
            near_ema_bull = (close >= ema_pb) and (close <= ema_pb + 1.5 * atr)
            near_ema_bear = (close <= ema_pb) and (close >= ema_pb - 1.5 * atr)

            signal = None
            reason = ""

            if adx_trending and (plus_di - minus_di) >= self.di_spread_min and near_ema_bull:
                signal = "BUY"
                reason = (
                    f"ADX {adx_now:.1f} (was {adx_old:.1f}) rising, "
                    f"+DI({plus_di:.1f}) spread {plus_di - minus_di:.1f} ≥ {self.di_spread_min}, "
                    f"price at EMA{self.ema_pullback} pullback zone"
                )
            elif adx_trending and (minus_di - plus_di) >= self.di_spread_min and near_ema_bear:
                signal = "SELL"
                reason = (
                    f"ADX {adx_now:.1f} (was {adx_old:.1f}) rising, "
                    f"-DI({minus_di:.1f}) spread {minus_di - plus_di:.1f} ≥ {self.di_spread_min}, "
                    f"price at EMA{self.ema_pullback} pullback zone"
                )

            if signal is None:
                continue

            sl_dist = atr * self.atr_sl_buffer
            if signal == "BUY":
                sl     = round(ema_pb - sl_dist, 2)
                risk   = round(close - sl, 2)
                target = round(close + risk * self.rr_target, 2)
            else:
                sl     = round(ema_pb + sl_dist, 2)
                risk   = round(sl - close, 2)
                target = round(close - risk * self.rr_target, 2)

            signals.append({
                "date":     row.name if hasattr(row, "name") else df.index[i],
                "signal":   signal,
                "entry":    round(close, 2),
                "sl":       sl,
                "target":   target,
                "risk":     round(risk, 2),
                "adx":      round(adx_now, 2),
                "plus_di":  round(plus_di, 2),
                "minus_di": round(minus_di, 2),
                "di_gap":   round(plus_di - minus_di, 2),
                "atr":      round(atr, 2),
                f"ema_{self.ema_pullback}": round(ema_pb, 2),
                "reason":   reason,
            })

        return signals