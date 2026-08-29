import pandas as pd

df = pd.read_csv("resources/instrument_list_x.csv")

# Filter only EQ instruments (excludes futures/options noise)
eq = df[df["instrument_type"] == "EQ"]

watchlist_symbols = [
    "LTIM", "MPHASIS", "FEDERALBNK", "IDFCFIRSTB", "MUTHOOTFIN",
    "GAIL", "ADANIGREEN", "APOLLOHOSP", "BEL", "CUMMINSIND",
    "CDSL"
]


def get_token():
    results = eq[eq["tradingsymbol"].isin(watchlist_symbols)][
        ["tradingsymbol", "instrument_token", "name"]
    ].sort_values("tradingsymbol")
    return results


# Single lookup
print(get_token().to_string(index=False))