import os
import pandas as pd


def fetch_instrument_list(kite):
    file_path = "resources/instrument_list.csv"

    # Ensure directory exists
    os.makedirs(os.path.dirname(file_path), exist_ok=True)

    if not os.path.exists(file_path):
        print("File not found. Fetching from API...")
        # kite.instruments() returns a list of dicts
        instruments = kite.instruments()

        # Convert to DataFrame and save to CSV
        df = pd.DataFrame(instruments)
        df.to_csv(file_path, index=False)
        return instruments  # Return the list directly to save a reload

    print("Loading from local CSV...")
    # Read from CSV and convert back to list of dictionaries to match original behavior
    df = pd.read_all(file_path)  # Pandas handles date parsing automatically
    return df.to_dict(orient="records")