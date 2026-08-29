from datetime import datetime, timedelta
import logging
import pandas as pd
from dotenv import load_dotenv

# Initialize Environment & Logging
load_dotenv()
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

from trading.database import get_db
from trading.user_token import fetch_user_token

# Constants
INDEX_TRADING_SYMBOL = "NIFTY 50"
INDEX_EXCHANGE = "NSE"
NIFTY_TOKEN = 256265  # NIFTY 50 NSE Instrument Token


class KiteIndexAnalyzer:
    def __init__(self, kite):
        self.kite = kite

    def fetch_candles(self, instrument_token: int, interval: str, days_back: int) -> pd.DataFrame:
        """Fetches OHLC data from Kite Connect API and converts it to a DataFrame."""
        to_date = datetime.now()
        from_date = to_date - timedelta(days=days_back)

        try:
            records = self.kite.historical_data(
                instrument_token=instrument_token,
                from_date=from_date.strftime("%Y-%m-%d %H:%M:%S"),
                to_date=to_date.strftime("%Y-%m-%d %H:%M:%S"),
                interval=interval
            )
            df = pd.DataFrame(records)
            if not df.empty:
                df['date'] = pd.to_datetime(df['date'])
                df.set_index('date', inplace=True)
            return df
        except Exception as e:
            logger.error(f"Error fetching historical data for interval {interval}: {e}")
            return pd.DataFrame()

    def get_3hr_trend(self, token: int) -> dict:
        """
        Calculates 3-Hour Trend Direction.
        Since Kite API native interval is 60minute, we aggregate 60min candles into 3-hour candles.
        """
        df_60m = self.fetch_candles(token, "60minute", days_back=10)
        if df_60m.empty:
            return {"direction": "NEUTRAL", "reason": "No data"}

        # Resample 60-min to 3-hour candles
        df_3h = df_60m.resample('3h').agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum'
        }).dropna()

        if len(df_3h) < 2:
            return {"direction": "NEUTRAL", "reason": "Insufficient 3H candles"}

        latest = df_3h.iloc[-1]
        prev = df_3h.iloc[-2]

        # Trend Logic: Bullish/Bearish based on Close and High/Low Breaks
        if latest['close'] > prev['high'] or latest['close'] > latest['open']:
            direction = "BULLISH"
        elif latest['close'] < prev['low'] or latest['close'] < latest['open']:
            direction = "BEARISH"
        else:
            direction = "NEUTRAL"

        return {
            "direction": direction,
            "last_3h_close": latest['close'],
            "3h_high": latest['high'],
            "3h_low": latest['low']
        }

    def get_1hr_levels(self, token: int) -> dict:
        """Calculates Support & Resistance levels using 1-hour candles via Pivot Points."""
        df_1h = self.fetch_candles(token, "60minute", days_back=5)
        if df_1h.empty or len(df_1h) < 2:
            return {}

        prev_candle = df_1h.iloc[-2]

        pivot = (prev_candle['high'] + prev_candle['low'] + prev_candle['close']) / 3
        r1 = (2 * pivot) - prev_candle['low']
        s1 = (2 * pivot) - prev_candle['high']
        r2 = pivot + (prev_candle['high'] - prev_candle['low'])
        s2 = pivot - (prev_candle['high'] - prev_candle['low'])

        return {
            "pivot": round(pivot, 2),
            "r1": round(r1, 2),
            "s1": round(s1, 2),
            "r2": round(r2, 2),
            "s2": round(s2, 2)
        }

    def evaluate_15min_signal(self, token: int, trend_3h: str, levels: dict) -> dict:
        """
        Analyzes the 15-minute candle for CE / PE timing based on price action
        confluence with 1-hour support/resistance and 3-hour market direction.
        """
        df_15m = self.fetch_candles(token, "15minute", days_back=3)
        if df_15m.empty or len(df_15m) < 2:
            return {"action": "NO_SIGNAL", "reason": "Insufficient 15m data"}

        latest = df_15m.iloc[-1]
        prev = df_15m.iloc[-2]
        current_price = latest['close']

        # Entry Signals
        is_bullish_engulfing = (latest['close'] > prev['open']) and (latest['open'] < prev['close'])
        is_bearish_engulfing = (latest['close'] < prev['open']) and (latest['open'] > prev['close'])

        # 1. Buy CE (Call Option) Setup
        if trend_3h == "BULLISH":
            # Bounce near S1/Pivot with bullish price action
            near_support = (abs(current_price - levels.get('s1', 0)) <= 25) or (
                        abs(current_price - levels.get('pivot', 0)) <= 25)
            if near_support or is_bullish_engulfing:
                return {
                    "action": "BUY_CE",
                    "price": current_price,
                    "target": levels.get('r1'),
                    "stop_loss": min(latest['low'], prev['low']),
                    "reason": f"3H Bullish Trend + 15M trigger near Support/Pivot ({levels.get('s1')})"
                }

        # 2. Buy PE (Put Option) Setup
        elif trend_3h == "BEARISH":
            # Rejection near R1/Pivot with bearish price action
            near_resistance = (abs(current_price - levels.get('r1', 0)) <= 25) or (
                        abs(current_price - levels.get('pivot', 0)) <= 25)
            if near_resistance or is_bearish_engulfing:
                return {
                    "action": "BUY_PE",
                    "price": current_price,
                    "target": levels.get('s1'),
                    "stop_loss": max(latest['high'], prev['high']),
                    "reason": f"3H Bearish Trend + 15M trigger near Resistance/Pivot ({levels.get('r1')})"
                }

        return {
            "action": "WAIT",
            "price": current_price,
            "reason": "Market in range or no clear 15M trigger"
        }

    def run_analysis(self, token: int):
        """Runs full top-down analysis execution flow."""
        logger.info(f"--- Running Market Analysis for Token: {token} ---")

        # 1. Check 3H Trend
        trend_info = self.get_3hr_trend(token)
        logger.info(f"3-Hour Trend Direction: {trend_info['direction']}")

        # 2. Calculate 1H Support / Resistance
        levels = self.get_1hr_levels(token)
        logger.info(f"1-Hour Key Levels: {levels}")

        # 3. Process 15M Candle Trigger
        signal = self.evaluate_15min_signal(token, trend_info['direction'], levels)
        logger.info(f"15-Minute Signal Decision: {signal}")

        return {
            "trend": trend_info,
            "levels": levels,
            "signal": signal
        }


# --- Entry Point ---
if __name__ == "__main__":
    # Fetch user session via token fetcher
    kite, user_id = fetch_user_token(logger)

    with get_db() as db:
        analyzer = KiteIndexAnalyzer(kite)

        # Analyze NIFTY 50 Index
        analysis_result = analyzer.run_analysis(NIFTY_TOKEN)

        print("\n===== ANALYSIS SUMMARY =====")
        print(f"Index Trend (3H): {analysis_result['trend']['direction']}")
        print(f"Pivot Levels (1H): {analysis_result['levels']}")
        print(f"Signal (15M): {analysis_result['signal']['action']}")
        print(f"Reason: {analysis_result['signal']['reason']}")
        print("============================\n")