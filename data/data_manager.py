import os
import pandas as pd
import datetime

DATA_DIR = "resources/historical_data"

if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)


def get_data(kite, symbol, token, days=365):
    file_path = os.path.join(DATA_DIR, f"{symbol}.csv")

    # Check if data exists on disk
    if os.path.exists(file_path):
        print(f"  -> Loading {symbol} from local cache...")
        return pd.read_csv(file_path)

    # Otherwise fetch from Kite
    print(f"  -> Fetching {symbol} from Kite API...")
    end_date = datetime.datetime.now()
    start_date = end_date - datetime.timedelta(days=days)

    raw_records = kite.historical_data(
        instrument_token=token,
        from_date=start_date.strftime("%Y-%m-%d"),
        to_date=end_date.strftime("%Y-%m-%d"),
        interval="day"
    )

    df = pd.DataFrame(raw_records)
    # Save to CSV for next time
    df.to_csv(file_path, index=False)
    return df


def get_data_lower(kite, symbol, token, days=365):
    file_path = os.path.join(DATA_DIR, f"{symbol}_15min.csv")

    # Check if data exists on disk
    if os.path.exists(file_path):
        print(f"-> Loading {symbol} 15-min data from local cache...")
        df = pd.read_csv(file_path)
        df['date'] = pd.to_datetime(df['date'])
        return df

    print(f"-> Fetching {symbol} 15-min data from Kite API (Chunked)...")
    end_date = datetime.datetime.now()
    start_date = end_date - datetime.timedelta(days=days)

    all_records = []
    current_end = end_date

    # 15-minute interval limit is 200 days. We fetch in 60-day chunks to be safe.
    while current_end > start_date:
        current_start = max(current_end - datetime.timedelta(days=60), start_date)
        print(f"   Fetching from {current_start.strftime('%Y-%m-%d')} to {current_end.strftime('%Y-%m-%d')}...")

        try:
            raw_records = kite.historical_data(
                instrument_token=token,
                from_date=current_start.strftime("%Y-%m-%d %H:%M:%S"),
                to_date=current_end.strftime("%Y-%m-%d %H:%M:%S"),
                interval="15minute"
            )
            if raw_records:
                all_records.extend(raw_records)
        except Exception as e:
            print(f"Error fetching chunk: {e}")
            break

        current_end = current_start - datetime.timedelta(seconds=1)
        time.sleep(0.5)  # Rate limiting safety margin

    if not all_records:
        raise ValueError("No records retrieved from Kite API.")

    # Process and remove duplicates (due to overlapping boundaries)
    df = pd.DataFrame(all_records)
    df['date'] = pd.to_datetime(df['date'])
    df.sort_values('date', inplace=True)
    df.drop_duplicates(subset=['date'], inplace=True)
    df.reset_index(drop=True, inplace=True)

    # Save to CSV for next time
    df.to_csv(file_path, index=False)
    return df