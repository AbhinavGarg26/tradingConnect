import pandas as pd
import numpy as np

from indicators import compute_rsi


def generate_signals(df):
    # 1. Base technical parameters
    df = compute_rsi(df)
    df['candle_body'] = (df['close'] - df['open']).abs()

    # 2. Tracking Consecutive Higher High Counts
    high_counts = [0] * len(df)
    count = 0
    for i in range(1, len(df)):
        if df['high'].iloc[i] > df['high'].iloc[i - 1]:
            count += 1
        else:
            count = 0
        high_counts[i] = count
    df['high_count'] = high_counts

    # 3. Signals Vectors
    # "cautious" flag sets off if counting range lands between 6 and 9
    df['cautious_zone'] = df['high_count'].between(6, 9)
    df['signal'] = "HOLD"

    for i in range(1, len(df)):
        prev_close = df['close'].iloc[i - 1]
        prev_open = df['open'].iloc[i - 1]
        curr_close = df['close'].iloc[i].astype(float)
        curr_open = df['open'].iloc[i].astype(float)

        # Check if the previous candle was a "Big Candle" (>= 20 points)
        is_big_bull = (prev_close - prev_open) >= 20
        is_big_bear = (prev_open - prev_close) >= 20

        # Bull Mode: Big Bullish candle + Current candle continues/engulfs upwards
        if is_big_bull and (curr_close > prev_close):
            if not df['cautious_zone'].iloc[i]:
                df.at[i, 'signal'] = 'BUY'
            else:
                df.at[i, 'signal'] = 'REVERSAL_WARN_BUY'

        # Bear Mode: Big Bearish candle + Current candle continues/engulfs downwards
        elif is_big_bear and (curr_close < prev_close):
            if not df['cautious_zone'].iloc[i]:
                df.at[i, 'signal'] = 'SELL'
            else:
                df.at[i, 'signal'] = 'REVERSAL_WARN_SELL'

    return df