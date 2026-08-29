import logging
import os
import pandas as pd
from datetime import datetime, timedelta

from data.data_manager import DATA_DIR
from scanner_divergence import WATCHLIST
from strategies.adx_crossover import apply_adx_crossover_strategy
from trading.user_token import fetch_user_token

# Import your optimized strategy function

INTERVAL = "day"  # Change to "60minute" or "minute" if you shift to intraday scans

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("divergence_scanner.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

def sync_local_cache(item: dict, kite_client) -> pd.DataFrame:
    """
    Loads data from cache. If data is > 2 days old or missing, 
    fetches increments via Kite Connect and updates the local file.
    """
    symbol = item["tradingsymbol"]
    token = item["instrument_token"]
    file_path = os.path.join(DATA_DIR, f"{symbol}.csv")
    
    df = pd.DataFrame()
    cache_exists = os.path.exists(file_path)
    
    if cache_exists:
        try:
            df = pd.read_csv(file_path)
            df['date'] = pd.to_datetime(df['date'])
            df = df.sort_values('date').reset_index(drop=True)
        except Exception as e:
            print(f"⚠️ Cache file for {symbol} corrupted. Re-fetching baseline... Error: {e}")
            cache_exists = False

    # Current execution time
    now = datetime.now()
    
    if cache_exists and not df.empty:
        last_cached_date = df['date'].max()
        # Convert to native naive timestamp for an accurate delta comparison
        if last_cached_date.tzinfo is not None:
            last_cached_date = last_cached_date.tz_localize(None)
            
        days_stale = (now - last_cached_date).days
        
        if days_stale > 2:
            print(f"🔄 {symbol} cache is {days_stale} days stale. Syncing missing rows...")
            from_date = last_cached_date + timedelta(days=1)
            
            try:
                # Fetching incremental block from Kite
                new_records = kite_client.historical_data(token, from_date.date(), now.date(), INTERVAL)
                if new_records:
                    new_df = pd.DataFrame(new_records)
                    new_df['date'] = pd.to_datetime(new_df['date'])
                    
                    # Stitch, clean up, and commit to disk
                    df = pd.concat([df, new_df]).drop_duplicates(subset=['date']).reset_index(drop=True)
                    df.to_csv(file_path, index=False)
                    print(f"✅ {symbol} cache successfully synced up to date.")
            except Exception as e:
                print(f"❌ Failed to sync live data for {symbol}: {e}")
    else:
        # Complete full data fetch baseline if cache doesn't exist
        print(f"📥 No cache found for {symbol}. Fetching 450-day history baseline for SMA 200...")
        from_date = now - timedelta(days=450) 
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            records = kite_client.historical_data(token, from_date.date(), now.date(), INTERVAL)
            if records:
                df = pd.DataFrame(records)
                df['date'] = pd.to_datetime(df['date'])
                df.to_csv(file_path, index=False)
                print(f"✅ Created brand new cache database for {symbol}")
        except Exception as e:
            print(f"❌ Failed initialization fetch for {symbol}: {e}")
            
    return df

def execute_live_scan(kite_client):
    print(f"🚀 RUNNING ACTIVE SCANNER PIPELINE | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 75)
    
    active_signals = []
    
    for item in WATCHLIST:
        symbol = item["tradingsymbol"]
        
        # 1. Sync cache automatically if required
        df = sync_local_cache(item, kite_client)
        
        if df is None or df.empty:
            continue
            
        # 2. Run your specific Alpha strategy function
        df_analyzed = apply_adx_crossover_strategy(df)
        
        if df_analyzed.empty or 'signal' not in df_analyzed.columns:
            continue
            
        # Get latest current state row
        latest_row = df_analyzed.iloc[-1]
        prev_row = df_analyzed.iloc[-2] if len(df_analyzed) > 1 else latest_row
        
        current_signal = latest_row['signal']
        
        if current_signal != 0:
            status = "🚨 NEW SETUP" if current_signal != prev_row['signal'] else "Sustained"
            direction = "LONG 🟢" if current_signal == 1 else "SHORT 🔴"

            # --- SAFE EXTRACT BLOCK ---
            raw_stop = latest_row['long_stop'] if current_signal == 1 else latest_row['short_stop']
            # If the stop value is valid, round it; otherwise, default to 0.0
            stop_loss = round(raw_stop, 2) if pd.notna(raw_stop) else 0.0
            
            active_signals.append({
                "Symbol": symbol,
                "Direction": direction,
                "State": status,
                "Price": round(latest_row['close'], 2),
                "ADX": round(latest_row.get('ADX', 0), 2),
                "Dynamic Stop": stop_loss
            })

    # Display Dashboard Results
    print("\n📊 LIVE INSTRUMENT MATRIX:")
    if active_signals:
        print(pd.DataFrame(active_signals).to_markdown(index=False))
    else:
        print("📝 All items verified. No new mathematical entry signals matched filters on current candles.")
    print("=" * 75)

if __name__ == "__main__":
    # Standard placeholder instantiation for context. 
    # Replace 'kite_session' with whatever your initialized KiteConnect client object instance is called.
    # from your_auth_script import kite_session
    kite, user_id = fetch_user_token(log)
    execute_live_scan(kite)
