def calculate_macd(df, fast=12, slow=26, signal=9):
    """Calculates MACD line and Signal line."""
    ema_fast = df['close'].ewm(span=fast, adjust=False).mean()
    ema_slow = df['close'].ewm(span=slow, adjust=False).mean()
    df['macd_line'] = ema_fast - ema_slow
    df['signal_line'] = df['macd_line'].ewm(span=signal, adjust=False).mean()
    return df