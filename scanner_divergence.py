"""
╔══════════════════════════════════════════════════════════════════════╗
║   DIVERGENCE SCANNER v2  —  Kite Connect                           ║
║                                                                      ║
║   Layers of confluence (stacks up like this):                       ║
║   ① MACD line divergence          → GOOD                           ║
║   ② MACD line + RSI divergence    → BEST                           ║
║   ③ MACD line + RSI + VWAP        → SUPERB                         ║
║                                                                      ║
║   NEW: MACD Histogram divergence (the pattern you described)        ║
║   Bullish: price lower low → histogram spike up → crossover →       ║
║            second spike up but SMALLER than first                   ║
║   Bearish: price higher high → histogram spike down → crossover →  ║
║            second spike down but SMALLER than first                 ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import os
import time
import logging
from datetime import datetime, timedelta
from typing import Optional
import pandas as pd
import numpy as np
from dotenv import load_dotenv
load_dotenv()

from trading.user_token import fetch_user_token

# ─── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("divergence_scanner.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
#  KITE CONNECT SETUP
# ═══════════════════════════════════════════════════════════════════════════════
kite, user_id = fetch_user_token(log)


# ═══════════════════════════════════════════════════════════════════════════════
#  WATCHLIST
#  ─ To find instrument_token for any symbol:
#      instruments = kite.instruments("NSE")
#      df = pd.DataFrame(instruments)
#      print(df[df["tradingsymbol"] == "RELIANCE"][["tradingsymbol","instrument_token"]])
# ═══════════════════════════════════════════════════════════════════════════════

WATCHLIST = [
    # ── Indices ──────────────────────────────────────────────────────────────
    {"tradingsymbol": "NIFTY 50",       "exchange": "NSE", "instrument_token": 256265},
    {"tradingsymbol": "NIFTY BANK",     "exchange": "NSE", "instrument_token": 260105},
    {"tradingsymbol": "NIFTY FIN SERVICE","exchange":"NSE","instrument_token": 257801},
    {"tradingsymbol": "NIFTY MIDCAP 100", "exchange": "NSE", "instrument_token": 288009},

    # ── Large Cap Stocks (NSE) ────────────────────────────────────────────────
    {"tradingsymbol": "TATASTEEL", "exchange": "NSE", "instrument_token": 895745},
    {"tradingsymbol": "BHARTIARTL", "exchange": "NSE", "instrument_token": 2714625},
    {"tradingsymbol": "IOC", "exchange": "NSE", "instrument_token": 415745},
    {"tradingsymbol": "ANGELONE", "exchange": "NSE", "instrument_token": 82945},
    {"tradingsymbol": "MANKIND", "exchange": "NSE", "instrument_token": 3937281},
    {"tradingsymbol": "VEDL", "exchange": "NSE", "instrument_token": 784129},
    {"tradingsymbol": "PFC", "exchange": "NSE", "instrument_token": 3660545},
    {"tradingsymbol": "MCX", "exchange": "NSE", "instrument_token": 7982337},
    {"tradingsymbol": "HINDALCO", "exchange": "NSE", "instrument_token": 348929},
    {"tradingsymbol": "ABB", "exchange": "NSE", "instrument_token": 3329},
    {"tradingsymbol": "MAZDOCK", "exchange": "NSE", "instrument_token": 130305},
    {"tradingsymbol": "RELIANCE",        "exchange": "NSE", "instrument_token": 738561},
    {"tradingsymbol": "HDFCBANK",        "exchange": "NSE", "instrument_token": 341249},
    {"tradingsymbol": "INFY",            "exchange": "NSE", "instrument_token": 408065},
    {"tradingsymbol": "TCS",             "exchange": "NSE", "instrument_token": 2953217},
    {"tradingsymbol": "ICICIBANK",       "exchange": "NSE", "instrument_token": 1270529},
    {"tradingsymbol": "SBIN",            "exchange": "NSE", "instrument_token": 779521},
    {"tradingsymbol": "AXISBANK",        "exchange": "NSE", "instrument_token": 1510401},
    {"tradingsymbol": "BAJFINANCE",      "exchange": "NSE", "instrument_token": 81153},
    {"tradingsymbol": "WIPRO",           "exchange": "NSE", "instrument_token": 969473},
    {"tradingsymbol": "ADANIENT",        "exchange": "NSE", "instrument_token": 6401},
    {"tradingsymbol": "MARUTI",          "exchange": "NSE", "instrument_token": 2815745},
    {"tradingsymbol": "HINDUNILVR",      "exchange": "NSE", "instrument_token": 356865},
    {"tradingsymbol": "KOTAKBANK",       "exchange": "NSE", "instrument_token": 492033},
    {"tradingsymbol": "LT",              "exchange": "NSE", "instrument_token": 2939649},
    {"tradingsymbol": "INDHOTEL",              "exchange": "NSE", "instrument_token": 387073},

    # ── IT / Tech ────────────────────────────────────────────────────────────
    {"tradingsymbol": "HCLTECH",   "exchange": "NSE", "instrument_token": 1850625},
    {"tradingsymbol": "TECHM",     "exchange": "NSE", "instrument_token": 3465729},
    {"tradingsymbol": "LTIM",      "exchange": "NSE", "instrument_token": 4561409},
    {"tradingsymbol": "PERSISTENT","exchange": "NSE", "instrument_token": 4701441},
    {"tradingsymbol": "MPHASIS",   "exchange": "NSE", "instrument_token": 1152769},

    # ── Banking / NBFC ───────────────────────────────────────────────────────
    {"tradingsymbol": "INDUSINDBK","exchange": "NSE", "instrument_token": 1346049},
    {"tradingsymbol": "BANDHANBNK","exchange": "NSE", "instrument_token": 579329},
    {"tradingsymbol": "FEDERALBNK","exchange": "NSE", "instrument_token": 261889},
    {"tradingsymbol": "IDFCFIRSTB","exchange": "NSE", "instrument_token": 2863105},
    {"tradingsymbol": "BAJAJFINSV","exchange": "NSE", "instrument_token": 4268801},
    {"tradingsymbol": "CHOLAFIN",  "exchange": "NSE", "instrument_token": 300545},
    {"tradingsymbol": "MUTHOOTFIN","exchange": "NSE", "instrument_token": 6054401},

    # ── Energy / Oil & Gas ───────────────────────────────────────────────────
    {"tradingsymbol": "ONGC",      "exchange": "NSE", "instrument_token": 633601},
    {"tradingsymbol": "BPCL",      "exchange": "NSE", "instrument_token": 134657},
    {"tradingsymbol": "GAIL",      "exchange": "NSE", "instrument_token": 1207553},
    {"tradingsymbol": "POWERGRID", "exchange": "NSE", "instrument_token": 3834113},
    {"tradingsymbol": "NTPC",      "exchange": "NSE", "instrument_token": 2977281},
    {"tradingsymbol": "TATAPOWER", "exchange": "NSE", "instrument_token": 877057},
    {"tradingsymbol": "ADANIGREEN","exchange": "NSE", "instrument_token": 912129},
    {"tradingsymbol": "ADANIPOWER", "exchange": "NSE", "instrument_token": 4451329},

    # ── Pharma / Healthcare ──────────────────────────────────────────────────
    {"tradingsymbol": "SUNPHARMA", "exchange": "NSE", "instrument_token": 857857},
    {"tradingsymbol": "DRREDDY",   "exchange": "NSE", "instrument_token": 225537},
    {"tradingsymbol": "CIPLA",     "exchange": "NSE", "instrument_token": 177665},
    {"tradingsymbol": "DIVISLAB",  "exchange": "NSE", "instrument_token": 2800641},
    {"tradingsymbol": "APOLLOHOSP","exchange": "NSE", "instrument_token": 40193},

    # ── Auto / EV ────────────────────────────────────────────────────────────
    {"tradingsymbol": "TMPV","exchange": "NSE", "instrument_token": 884737},
    {"tradingsymbol": "M&M",       "exchange": "NSE", "instrument_token": 519937},
    {"tradingsymbol": "BAJAJ-AUTO","exchange": "NSE", "instrument_token": 4267265},
    {"tradingsymbol": "HEROMOTOCO","exchange": "NSE", "instrument_token": 345089},
    {"tradingsymbol": "EICHERMOT", "exchange": "NSE", "instrument_token": 232961},
    {"tradingsymbol": "TVSMOTOR",  "exchange": "NSE", "instrument_token": 2170625},

    # ── Metals / Mining ──────────────────────────────────────────────────────
    {"tradingsymbol": "COALINDIA", "exchange": "NSE", "instrument_token": 5215745},
    {"tradingsymbol": "NMDC",      "exchange": "NSE", "instrument_token": 3924993},
    {"tradingsymbol": "JSWSTEEL",  "exchange": "NSE", "instrument_token": 3001089},
    {"tradingsymbol": "SAIL",      "exchange": "NSE", "instrument_token": 758529},

    # ── Capital Goods / Defence ──────────────────────────────────────────────
    {"tradingsymbol": "BEL",       "exchange": "NSE", "instrument_token": 98049},
    {"tradingsymbol": "HAL",       "exchange": "NSE", "instrument_token": 589569},
    {"tradingsymbol": "BHEL",      "exchange": "NSE", "instrument_token": 112129},
    {"tradingsymbol": "SIEMENS",   "exchange": "NSE", "instrument_token": 806401},
    {"tradingsymbol": "CUMMINSIND","exchange": "NSE", "instrument_token": 486657},

    # ── FMCG / Consumer ──────────────────────────────────────────────────────
    {"tradingsymbol": "ITC",       "exchange": "NSE", "instrument_token": 424961},
    {"tradingsymbol": "NESTLEIND", "exchange": "NSE", "instrument_token": 4598529},
    {"tradingsymbol": "BRITANNIA", "exchange": "NSE", "instrument_token": 140033},
    {"tradingsymbol": "DABUR",     "exchange": "NSE", "instrument_token": 197633},
    {"tradingsymbol": "MARICO",    "exchange": "NSE", "instrument_token": 1041153},
    {"tradingsymbol": "VBL","exchange": "NSE", "instrument_token": 4843777},

    # ── Cement / Infra ───────────────────────────────────────────────────────
    {"tradingsymbol": "ULTRACEMCO","exchange": "NSE", "instrument_token": 2952193},
    {"tradingsymbol": "SHREECEM",  "exchange": "NSE", "instrument_token": 794369},
    {"tradingsymbol": "AMBUJACEM", "exchange": "NSE", "instrument_token": 1152769},
    {"tradingsymbol": "ACC",       "exchange": "NSE", "instrument_token": 5633},

    # ── Exchanges / Brokers / Fintech ────────────────────────────────────────
    {"tradingsymbol": "BSE",       "exchange": "NSE", "instrument_token": 5097729},
    {"tradingsymbol": "CDSL",      "exchange": "NSE", "instrument_token": 5420545},
    {"tradingsymbol": "ABCAPITAL",      "exchange": "NSE", "instrument_token": 5533185},
    {"tradingsymbol": "CAMS",      "exchange": "NSE", "instrument_token": 87553},
    {"tradingsymbol": "POLICYBZR", "exchange": "NSE", "instrument_token": 5552641},

    # ── Real Estate                     ────────────────────────────────────────
    {"tradingsymbol": "DLF",        "exchange": "NSE", "instrument_token": 377857},
    {"tradingsymbol": "GODREJPROP", "exchange": "NSE", "instrument_token": 4576001},
    {"tradingsymbol": "PHOENIXLTD", "exchange": "NSE", "instrument_token": 3725313},

    # ── Consumer Durables / Retail / Paints ────────────────────────────────────────
    {"tradingsymbol": "TITAN",      "exchange": "NSE", "instrument_token": 897537},
    {"tradingsymbol": "TRENT",      "exchange": "NSE", "instrument_token": 502785},
    {"tradingsymbol": "ASIANPAINT", "exchange": "NSE", "instrument_token": 60417},
    {"tradingsymbol": "HAVELLS",    "exchange": "NSE", "instrument_token": 251137},
    {"tradingsymbol": "BERGEPAINT", "exchange": "NSE", "instrument_token": 103425},
    {"tradingsymbol": "VOLTAS",     "exchange": "NSE", "instrument_token": 951809},
    {"tradingsymbol": "VBL",        "exchange": "NSE", "instrument_token": 4843777},
    {"tradingsymbol": "HAVELLS",       "exchange": "NSE", "instrument_token": 2513665},

    # ── New Age / Platform / Fintech ────────────────────────────────────────
    {"tradingsymbol": "ETERNAL",     "exchange": "NSE", "instrument_token": 1304833},
    {"tradingsymbol": "JIOFIN",     "exchange": "NSE", "instrument_token": 4644609},
    {"tradingsymbol": "PAYTM",      "exchange": "NSE", "instrument_token": 1716481},
    {"tradingsymbol": "NYKAA",      "exchange": "NSE", "instrument_token": 1675521},
    {"tradingsymbol": "DELHIVERY",  "exchange": "NSE", "instrument_token": 2457345},

    # ── Chemicals & Fertilizers ────────────────────────────────────────
    {"tradingsymbol": "PIDILITIND", "exchange": "NSE", "instrument_token": 681985},
    {"tradingsymbol": "SRF",        "exchange": "NSE", "instrument_token": 837889},
    {"tradingsymbol": "UPL",        "exchange": "NSE", "instrument_token": 2889473},
    {"tradingsymbol": "TATACHEM",   "exchange": "NSE", "instrument_token": 871681},

    # ── Aviation & Logistics ────────────────────────────────────────
    {"tradingsymbol": "INDIGO",     "exchange": "NSE", "instrument_token": 2865921},
    {"tradingsymbol": "CONCOR",     "exchange": "NSE", "instrument_token": 1215745},
    {"tradingsymbol": "BLUEDART",   "exchange": "NSE", "instrument_token": 126721},

    # ── Diversified / Others ────────────────────────────────────────
    {"tradingsymbol": "GRASIM",     "exchange": "NSE", "instrument_token": 315393},
    {"tradingsymbol": "TATACONSUM", "exchange": "NSE", "instrument_token": 878593},
    {"tradingsymbol": "POLYCAB",    "exchange": "NSE", "instrument_token": 2455041},
    {"tradingsymbol": "RECLTD",     "exchange": "NSE", "instrument_token": 3930881},

    # ── PSU Railway & Energy (Mid/Small) ────────────────────────────────────────
    {"tradingsymbol": "RVNL", "exchange": "NSE", "instrument_token": 2445313},
    {"tradingsymbol": "IRFC", "exchange": "NSE", "instrument_token": 519425},
    {"tradingsymbol": "SJVN", "exchange": "NSE", "instrument_token": 4834049},
    {"tradingsymbol": "IREDA", "exchange": "NSE", "instrument_token": 5186817},
    {"tradingsymbol": "NHPC", "exchange": "NSE", "instrument_token": 3677697},

    # ── Mid Cap Leaders (NSE) ────────────────────────────────────────
    {"tradingsymbol": "ASHOKLEY",   "exchange": "NSE", "instrument_token": 54273},
    {"tradingsymbol": "AUROPHARMA", "exchange": "NSE", "instrument_token": 70401},
    {"tradingsymbol": "BALKRISIND", "exchange": "NSE", "instrument_token": 1629185},
    {"tradingsymbol": "BHARATFORG", "exchange": "NSE", "instrument_token": 108033},
    {"tradingsymbol": "BIOCON",     "exchange": "NSE", "instrument_token": 2911489},
    {"tradingsymbol": "COLPAL",     "exchange": "NSE", "instrument_token": 3876097},
    {"tradingsymbol": "ESCORTS",    "exchange": "NSE", "instrument_token": 245249},
    {"tradingsymbol": "EXIDEIND",   "exchange": "NSE", "instrument_token": 173057},
    {"tradingsymbol": "GUJGASLTD",  "exchange": "NSE", "instrument_token": 2730497},
    {"tradingsymbol": "ADANIPORTS", "exchange": "NSE", "instrument_token": 3861249},
    {"tradingsymbol": "IPCALAB",    "exchange": "NSE", "instrument_token": 418049},
    {"tradingsymbol": "JINDALSTEL", "exchange": "NSE", "instrument_token": 1723649},
    {"tradingsymbol": "JUBLFOOD",   "exchange": "NSE", "instrument_token": 4632577},
    {"tradingsymbol": "LICHSGFIN",  "exchange": "NSE", "instrument_token": 511233},
    {"tradingsymbol": "LUPIN",      "exchange": "NSE", "instrument_token": 1640193},
    {"tradingsymbol": "MRF",        "exchange": "NSE", "instrument_token": 582913},
    {"tradingsymbol": "PAGEIND",    "exchange": "NSE", "instrument_token": 3689729},
    {"tradingsymbol": "SUNTV",      "exchange": "NSE", "instrument_token": 3431425},
    {"tradingsymbol": "TATAELXSI",  "exchange": "NSE", "instrument_token": 873217},
    {"tradingsymbol": "UBL",        "exchange": "NSE", "instrument_token": 4278529},
    {"tradingsymbol": "YESBANK",    "exchange": "NSE", "instrument_token": 3050241},

    # ── Small Cap / High Growth (NSE) ────────────────────────────────────────
    {"tradingsymbol": "SUZLON",     "exchange": "NSE", "instrument_token": 3076609},
    {"tradingsymbol": "TRIDENT",    "exchange": "NSE", "instrument_token": 2479361},
    {"tradingsymbol": "RTNINDIA","exchange": "NSE", "instrument_token": 6988033},
    {"tradingsymbol": "SOUTHBANK",  "exchange": "NSE", "instrument_token": 1522689},
    {"tradingsymbol": "HFCL",       "exchange": "NSE", "instrument_token": 5619457},
    {"tradingsymbol": "ZENSARTECH", "exchange": "NSE", "instrument_token": 275457},
    {"tradingsymbol": "SONATSOFTW", "exchange": "NSE", "instrument_token": 1688577},
    {"tradingsymbol": "KARURVYSYA", "exchange": "NSE", "instrument_token": 470529},
    {"tradingsymbol": "NBCC",       "exchange": "NSE", "instrument_token": 8042241},
    {"tradingsymbol": "IRCON",      "exchange": "NSE", "instrument_token": 1276417},
    {"tradingsymbol": "HUDCO",      "exchange": "NSE", "instrument_token": 5331201},
    {"tradingsymbol": "MAHABANK",   "exchange": "NSE", "instrument_token": 2912513},
    {"tradingsymbol": "IFCI",       "exchange": "NSE", "instrument_token": 381697},
    {"tradingsymbol": "ALOKINDS",   "exchange": "NSE", "instrument_token": 4524801}
]


# ═══════════════════════════════════════════════════════════════════════════════
#  TIMEFRAME CONFIG
# ═══════════════════════════════════════════════════════════════════════════════
TIMEFRAME_MAP = {
    "15minute": {"interval": "15minute", "lookback_days": 10},
    "60minute": {"interval": "60minute", "lookback_days": 30},
    "day":      {"interval": "day",      "lookback_days": 180},
    "week":     {"interval": "week",      "lookback_days": 1095},
}
DEFAULT_TIMEFRAMES = ["day", "week"]

# ═══════════════════════════════════════════════════════════════════════════════
#  PARAMETERS
# ═══════════════════════════════════════════════════════════════════════════════
# MACD
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
HIST_MAX_BARS = 30

# RSI
RSI_PERIOD = 14
RSI_OVERSOLD = 40  # for bullish divergence context (relaxed for early detection)
RSI_OVERBOUGHT = 60  # for bearish divergence context

# VWAP
VWAP_BAND_MULT = 1.5  # std deviations for VWAP bands

# Pivot detection
PIVOT_LOOKBACK = 5  # bars each side to confirm a swing pivot
MIN_DIV_BARS = 5  # minimum separation between two histogram spikes
MAX_DIV_BARS = 30  # ← 30-bar cap: divergence must complete within 30 bars
#   (on 15m = 7.5hrs, on 1H = 30hrs, on Day = 6 weeks)

# Histogram divergence thresholds
# spike2 must be LESS THAN this fraction of spike1's magnitude
# 0.75 means: spike1=-10 → spike2 must be shallower than -7.5  (25%+ weaker)
# This is your key filter — tighter = fewer but higher quality signals
HIST_SECOND_SPIKE_RATIO = 0.75  # ← 75% threshold as you specified

# Minimum absolute histogram spike size to qualify
# Filters out noise on small-magnitude histograms (e.g. 0.1 → 0.07 is meaningless)
# Set per-timeframe feel: 15m Nifty typically has histogram values of 5-50+
HIST_MIN_SPIKE_ABS = 0.5  # spike1 abs value must exceed this

# Crossover must be genuine — not a micro-blip through zero
HIST_CROSSOVER_MIN_DEPTH = 0.10  # ← raised to 10% of spike1 magnitude (was 5%)

# Minimum price strength between the two lows/highs
PRICE_STRENGTH_MIN = 0.002  # 0.2% price difference between pivot lows/highs

# How recent spike2 must be — the "about to happen or just happened" window
# 0 = spike2 is the very last bar (forming now)
# 5 = spike2 formed within last 5 bars (still fresh)
MAX_RECENCY_BARS = 5  # ← tightened from 15 to 5: fresh signals only


# ═══════════════════════════════════════════════════════════════════════════════
#  INDICATOR CALCULATIONS
# ═══════════════════════════════════════════════════════════════════════════════

def compute_macd(df: pd.DataFrame) -> pd.DataFrame:
    """Adds macd, signal, histogram columns."""
    df = df.copy()
    ema_fast = df["close"].ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow = df["close"].ewm(span=MACD_SLOW, adjust=False).mean()
    df["macd"] = ema_fast - ema_slow
    df["signal"] = df["macd"].ewm(span=MACD_SIGNAL, adjust=False).mean()
    df["histogram"] = df["macd"] - df["signal"]
    return df


def compute_rsi(df: pd.DataFrame) -> pd.DataFrame:
    """Adds rsi column."""
    df = df.copy()
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=RSI_PERIOD - 1, adjust=False).mean()
    avg_loss = loss.ewm(com=RSI_PERIOD - 1, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    df["rsi"] = 100 - (100 / (1 + rs))
    return df


def compute_vwap(df: pd.DataFrame) -> pd.DataFrame:
    """
    Adds vwap and vwap_upper / vwap_lower band columns.
    VWAP resets each trading day.
    """
    df = df.copy()
    df["typical_price"] = (df["high"] + df["low"] + df["close"]) / 3
    df["tp_vol"] = df["typical_price"] * df["volume"]
    df["tp2_vol"] = (df["typical_price"] ** 2) * df["volume"]

    # Group by date for daily reset
    dates = df.index.normalize() if hasattr(df.index, "normalize") else pd.Series(df.index).dt.normalize()
    df["_date"] = dates.values

    vwap_vals = []
    upper_vals = []
    lower_vals = []

    for date, group in df.groupby("_date"):
        cum_tp_vol = group["tp_vol"].cumsum()
        cum_vol = group["volume"].cumsum()
        cum_tp2_vol = group["tp2_vol"].cumsum()

        vwap = cum_tp_vol / cum_vol.replace(0, 1e-10)
        variance = (cum_tp2_vol / cum_vol.replace(0, 1e-10)) - vwap ** 2
        std = variance.clip(lower=0).apply(np.sqrt)

        vwap_vals.extend(vwap.tolist())
        upper_vals.extend((vwap + VWAP_BAND_MULT * std).tolist())
        lower_vals.extend((vwap - VWAP_BAND_MULT * std).tolist())

    df["vwap"] = vwap_vals
    df["vwap_upper"] = upper_vals
    df["vwap_lower"] = lower_vals
    df.drop(columns=["typical_price", "tp_vol", "tp2_vol", "_date"], inplace=True)
    return df


def compute_all(df: pd.DataFrame) -> pd.DataFrame:
    """Compute MACD + RSI + VWAP in one call."""
    df = compute_macd(df)
    df = compute_rsi(df)
    if "volume" in df.columns and df["volume"].sum() > 0:
        try:
            df = compute_vwap(df)
        except Exception as e:
            log.warning(f"VWAP calc failed: {e}")
            df["vwap"] = df["vwap_upper"] = df["vwap_lower"] = np.nan
    else:
        df["vwap"] = df["vwap_upper"] = df["vwap_lower"] = np.nan
    return df


# ═══════════════════════════════════════════════════════════════════════════════
#  PIVOT UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

def pivot_lows(series: pd.Series, n: int = PIVOT_LOOKBACK) -> list:
    """Return integer positions of confirmed pivot lows."""
    positions = []
    arr = series.values
    for i in range(n, len(arr) - n):
        if arr[i] == arr[i - n: i + n + 1].min():
            positions.append(i)
    return positions


def pivot_highs(series: pd.Series, n: int = PIVOT_LOOKBACK) -> list:
    """Return integer positions of confirmed pivot highs."""
    positions = []
    arr = series.values
    for i in range(n, len(arr) - n):
        if arr[i] == arr[i - n: i + n + 1].max():
            positions.append(i)
    return positions


# ═══════════════════════════════════════════════════════════════════════════════
#  HISTOGRAM DIVERGENCE  —  CORRECT PATTERN (as Abhinav described)
#
#  BULLISH histogram divergence — strict left-to-right sequence:
#
#  ① price_low_1  (e.g. 100)
#       Price makes a swing low. Selling was strong.
#  ② hist_spike_1 NEGATIVE  (e.g. -8)
#       Histogram dips deep below zero — sellers dominated that move.
#  ③ Histogram crosses UP above zero  (the crossover)
#       Price bounces. Buyers temporarily in control.
#  ④ Histogram crosses back DOWN below zero again
#       Sellers try again — price makes a new low.
#  ⑤ price_low_2  (e.g. 90)  ← LOWER than low_1
#       Price actually went lower... but look at the histogram:
#  ⑥ hist_spike_2 NEGATIVE but SHALLOWER  (e.g. -3)
#       Sellers pushed price lower but their histogram momentum is far weaker.
#       This is the divergence: price lower, selling power LESS → reversal likely.
#
#  BEARISH histogram divergence — exact mirror:
#  price_high_1 → hist_spike_1 POSITIVE (+8) → crossover DOWN → crossover UP →
#  price_high_2 (higher) → hist_spike_2 POSITIVE but SMALLER (+3)
#  Buyers pushed price higher but with less momentum → reversal down likely.
#
#  KEY INSIGHT: The negative spikes align with price LOWS (selling pressure).
#               Shallower 2nd spike = sellers exhausted = bullish divergence.
# ═══════════════════════════════════════════════════════════════════════════════

def detect_histogram_divergence(df: pd.DataFrame) -> list[dict]:
    """
    Detects MACD histogram divergence with all quality filters applied.

    BULLISH: two NEGATIVE spikes around price lows.
      spike1 deep (e.g. -10), spike2 shallower (must be < -7.5 if ratio=0.75)
      Real positive crossover between them. spike2 within 30 bars of spike1.
      Price made a lower low at spike2 despite weaker selling histogram.

    BEARISH: two POSITIVE spikes around price highs. Mirror of above.

    Quality filters applied:
      ① 30-bar cap between spikes (MAX_DIV_BARS)
      ② 75% ratio: spike2 must be < 75% of spike1's magnitude (HIST_SECOND_SPIKE_RATIO)
      ③ Minimum spike absolute size (HIST_MIN_SPIKE_ABS) — no noise signals
      ④ Crossover must be genuine depth (HIST_CROSSOVER_MIN_DEPTH = 10% of spike1)
      ⑤ spike2 must be fresh — within MAX_RECENCY_BARS of current bar
    """
    results = []
    hist = df["histogram"].values
    lows = df["low"].values
    highs = df["high"].values
    n = len(hist)

    def neg_troughs(h):
        """Local minima strictly below zero — these are the selling pressure peaks."""
        out = []
        for i in range(1, len(h) - 1):
            if h[i] < 0 and h[i] < h[i - 1] and h[i] < h[i + 1]:
                out.append(i)
        return out

    def pos_peaks(h):
        """Local maxima strictly above zero — these are the buying pressure peaks."""
        out = []
        for i in range(1, len(h) - 1):
            if h[i] > 0 and h[i] > h[i - 1] and h[i] > h[i + 1]:
                out.append(i)
        return out

    all_neg_troughs = neg_troughs(hist)
    all_pos_peaks = pos_peaks(hist)

    # ─────────────────────────────────────────────────────────────────────────
    # BULLISH HISTOGRAM DIVERGENCE
    # Two negative troughs. spike1 deeper, spike2 shallower.
    # Real crossover above zero between them. Price lower low.
    # ─────────────────────────────────────────────────────────────────────────
    for j in range(1, len(all_neg_troughs)):
        spike1_bar = all_neg_troughs[j - 1]
        spike2_bar = all_neg_troughs[j]

        h1 = hist[spike1_bar]  # e.g. -10  (negative, deeper)
        h2 = hist[spike2_bar]  # e.g. -3   (negative, shallower)

        # ① Both must be negative
        if h1 >= 0 or h2 >= 0:
            continue

        # ③ spike1 must meet minimum absolute magnitude — no noise
        #    e.g. spike1=-0.3 on a symbol with histogram range of ±50 is meaningless
        if abs(h1) < HIST_MIN_SPIKE_ABS:
            continue

        # ② spike2 must be SHALLOWER than spike1 (less negative)
        #    abs(h2) < abs(h1)  →  h2 > h1 since both are negative
        if h2 <= h1:
            continue  # spike2 is same depth or deeper — no divergence

        # ② 75% threshold: spike2 must be < 75% of spike1's magnitude
        #    Example: spike1=-10 → spike2 must be shallower than -7.5
        #    i.e. abs(h2) < abs(h1) * 0.75
        if abs(h2) >= abs(h1) * HIST_SECOND_SPIKE_RATIO:
            continue  # not weak enough — difference is noise

        # ① 30-bar cap: spikes must be within MAX_DIV_BARS of each other
        bar_dist = spike2_bar - spike1_bar
        if not (MIN_DIV_BARS <= bar_dist <= MAX_DIV_BARS):
            continue

        # ④ Real positive crossover between the two negative spikes
        #    Histogram must rise above zero (buyers briefly take control)
        between = hist[spike1_bar + 1: spike2_bar]
        if len(between) == 0:
            continue

        highest_in_between = between.max()

        # Crossover must be a genuine rise — at least 10% of spike1's depth
        # This prevents a flat near-zero histogram counting as a crossover
        min_crossover_height = abs(h1) * HIST_CROSSOVER_MIN_DEPTH
        if highest_in_between < min_crossover_height:
            continue

        # ── Price: lower low ─────────────────────────────────────────────────
        w = PIVOT_LOOKBACK
        low1_slice = lows[max(0, spike1_bar - w): spike1_bar + w + 1]
        low2_slice = lows[max(0, spike2_bar - w): spike2_bar + w + 1]

        if len(low1_slice) == 0 or len(low2_slice) == 0:
            continue

        price_low1 = float(low1_slice.min())
        price_low2 = float(low2_slice.min())
        low1_bar = max(0, spike1_bar - w) + int(np.argmin(low1_slice))
        low2_bar = max(0, spike2_bar - w) + int(np.argmin(low2_slice))

        if price_low2 >= price_low1:
            continue  # price did not make a lower low — not a divergence

        price_strength = (price_low1 - price_low2) / price_low1
        if price_strength < PRICE_STRENGTH_MIN:
            continue

        # ⑤ spike2 must be fresh — within MAX_RECENCY_BARS of current bar
        recency = (n - 1) - spike2_bar
        if recency > MAX_RECENCY_BARS:
            continue

        weakness_pct = round((1 - abs(h2) / abs(h1)) * 100, 1)

        # Human-readable quality note
        if weakness_pct >= 60:
            quality_note = "very strong divergence"
        elif weakness_pct >= 40:
            quality_note = "strong divergence"
        else:
            quality_note = "moderate divergence"

        results.append({
            "type": "BULLISH HISTOGRAM",
            "sub_type": "histogram_divergence",
            "emoji": "📊🟢",
            "symbol": df.attrs.get("symbol", ""),
            "timeframe": df.attrs.get("timeframe", ""),
            "bias": "BUY",
            "price_low1": round(price_low1, 2),
            "price_low2": round(price_low2, 2),
            "low1_bar": low1_bar,
            "low2_bar": low2_bar,
            "hist_spike1": round(h1, 4),
            "hist_spike2": round(h2, 4),
            "hist_crossover_peak": round(float(highest_in_between), 4),
            "hist_weakness_pct": weakness_pct,
            "quality_note": quality_note,
            "spike1_bar": spike1_bar,
            "spike2_bar": spike2_bar,
            "recency_bars": recency,
            "strength_pct": round(price_strength * 100, 2),
            "bar_dist": bar_dist,
        })

    # ─────────────────────────────────────────────────────────────────────────
    # BEARISH HISTOGRAM DIVERGENCE
    # Two positive peaks. spike1 taller, spike2 shorter.
    # Real negative crossover between them. Price higher high.
    # ─────────────────────────────────────────────────────────────────────────
    for j in range(1, len(all_pos_peaks)):
        spike1_bar = all_pos_peaks[j - 1]
        spike2_bar = all_pos_peaks[j]

        h1 = hist[spike1_bar]  # e.g. +10
        h2 = hist[spike2_bar]  # e.g. +3

        # ① Both must be positive
        if h1 <= 0 or h2 <= 0:
            continue

        # ③ Minimum spike magnitude
        if h1 < HIST_MIN_SPIKE_ABS:
            continue

        # ② spike2 must be smaller than spike1
        if h2 >= h1:
            continue

        # ② 75% threshold
        if h2 >= h1 * HIST_SECOND_SPIKE_RATIO:
            continue

        # ① 30-bar cap
        bar_dist = spike2_bar - spike1_bar
        if not (MIN_DIV_BARS <= bar_dist <= MAX_DIV_BARS):
            continue

        # ④ Real negative crossover between the two positive spikes
        between = hist[spike1_bar + 1: spike2_bar]
        if len(between) == 0:
            continue

        lowest_in_between = between.min()
        min_crossover_depth = -h1 * HIST_CROSSOVER_MIN_DEPTH
        if lowest_in_between > min_crossover_depth:
            continue

        # ── Price: higher high ───────────────────────────────────────────────
        w = PIVOT_LOOKBACK
        high1_slice = highs[max(0, spike1_bar - w): spike1_bar + w + 1]
        high2_slice = highs[max(0, spike2_bar - w): spike2_bar + w + 1]

        if len(high1_slice) == 0 or len(high2_slice) == 0:
            continue

        price_high1 = float(high1_slice.max())
        price_high2 = float(high2_slice.max())
        high1_bar = max(0, spike1_bar - w) + int(np.argmax(high1_slice))
        high2_bar = max(0, spike2_bar - w) + int(np.argmax(high2_slice))

        if price_high2 <= price_high1:
            continue

        price_strength = (price_high2 - price_high1) / price_high1
        if price_strength < PRICE_STRENGTH_MIN:
            continue

        # ⑤ Freshness
        recency = (n - 1) - spike2_bar
        if recency > MAX_RECENCY_BARS:
            continue

        weakness_pct = round((1 - h2 / h1) * 100, 1)

        if weakness_pct >= 60:
            quality_note = "very strong divergence"
        elif weakness_pct >= 40:
            quality_note = "strong divergence"
        else:
            quality_note = "moderate divergence"

        results.append({
            "type": "BEARISH HISTOGRAM",
            "sub_type": "histogram_divergence",
            "emoji": "📊🔴",
            "symbol": df.attrs.get("symbol", ""),
            "timeframe": df.attrs.get("timeframe", ""),
            "bias": "SELL",
            "price_high1": round(price_high1, 2),
            "price_high2": round(price_high2, 2),
            "high1_bar": high1_bar,
            "high2_bar": high2_bar,
            "hist_spike1": round(h1, 4),
            "hist_spike2": round(h2, 4),
            "hist_crossover_trough": round(float(lowest_in_between), 4),
            "hist_weakness_pct": weakness_pct,
            "quality_note": quality_note,
            "spike1_bar": spike1_bar,
            "spike2_bar": spike2_bar,
            "recency_bars": recency,
            "strength_pct": round(price_strength * 100, 2),
            "bar_dist": bar_dist,
        })

    results.sort(key=lambda x: x["recency_bars"])
    return results


# ═══════════════════════════════════════════════════════════════════════════════
#  MACD LINE DIVERGENCE  (price pivots vs MACD line pivots)
# ═══════════════════════════════════════════════════════════════════════════════

def detect_macd_line_divergence(df: pd.DataFrame) -> list[dict]:
    """Detects classic MACD line divergence (all 4 types)."""
    results = []
    n = len(df)

    price_low_pos = pivot_lows(df["low"])
    price_high_pos = pivot_highs(df["high"])
    macd_low_pos = pivot_lows(df["macd"])
    macd_high_pos = pivot_highs(df["macd"])

    # ── BULLISH REGULAR: price lower low, MACD higher low ────────────────────
    for i in range(1, len(price_low_pos)):
        p2, p1 = price_low_pos[i], price_low_pos[i - 1]
        bar_dist = p2 - p1
        if not (MIN_DIV_BARS <= bar_dist <= MAX_DIV_BARS):
            continue

        price1 = df["low"].iloc[p1]
        price2 = df["low"].iloc[p2]
        if price2 >= price1:
            continue
        strength = (price1 - price2) / price1
        if strength < PRICE_STRENGTH_MIN:
            continue

        m_candidates = [m for m in macd_low_pos if p1 <= m <= p2]
        if len(m_candidates) < 2:
            continue
        m1, m2 = m_candidates[0], m_candidates[-1]
        macd1, macd2 = df["macd"].iloc[m1], df["macd"].iloc[m2]
        if macd2 <= macd1:
            continue

        recency = (n - 1) - p2
        if recency > MAX_RECENCY_BARS:
            continue

        results.append(_build_signal(
            "BULLISH REGULAR", "🟢", df, p1, p2, m1, m2,
            price1, price2, macd1, macd2, strength, recency, bar_dist, "BUY"
        ))

    # ── BEARISH REGULAR: price higher high, MACD lower high ──────────────────
    for i in range(1, len(price_high_pos)):
        p2, p1 = price_high_pos[i], price_high_pos[i - 1]
        bar_dist = p2 - p1
        if not (MIN_DIV_BARS <= bar_dist <= MAX_DIV_BARS):
            continue

        price1 = df["high"].iloc[p1]
        price2 = df["high"].iloc[p2]
        if price2 <= price1:
            continue
        strength = (price2 - price1) / price1
        if strength < PRICE_STRENGTH_MIN:
            continue

        m_candidates = [m for m in macd_high_pos if p1 <= m <= p2]
        if len(m_candidates) < 2:
            continue
        m1, m2 = m_candidates[0], m_candidates[-1]
        macd1, macd2 = df["macd"].iloc[m1], df["macd"].iloc[m2]
        if macd2 >= macd1:
            continue

        recency = (n - 1) - p2
        if recency > MAX_RECENCY_BARS:
            continue

        results.append(_build_signal(
            "BEARISH REGULAR", "🔴", df, p1, p2, m1, m2,
            price1, price2, macd1, macd2, strength, recency, bar_dist, "SELL"
        ))

    # ── BULLISH HIDDEN: price higher low, MACD lower low (uptrend continuation)
    for i in range(1, len(price_low_pos)):
        p2, p1 = price_low_pos[i], price_low_pos[i - 1]
        bar_dist = p2 - p1
        if not (MIN_DIV_BARS <= bar_dist <= MAX_DIV_BARS):
            continue

        price1 = df["low"].iloc[p1]
        price2 = df["low"].iloc[p2]
        if price2 <= price1:
            continue
        strength = (price2 - price1) / price1
        if strength < PRICE_STRENGTH_MIN:
            continue

        m_candidates = [m for m in macd_low_pos if p1 <= m <= p2]
        if len(m_candidates) < 2:
            continue
        m1, m2 = m_candidates[0], m_candidates[-1]
        macd1, macd2 = df["macd"].iloc[m1], df["macd"].iloc[m2]
        if macd2 >= macd1:
            continue

        recency = (n - 1) - p2
        if recency > MAX_RECENCY_BARS:
            continue

        results.append(_build_signal(
            "BULLISH HIDDEN", "🟡", df, p1, p2, m1, m2,
            price1, price2, macd1, macd2, strength, recency, bar_dist, "BUY (trend cont.)"
        ))

    # ── BEARISH HIDDEN: price lower high, MACD higher high (downtrend continuation)
    for i in range(1, len(price_high_pos)):
        p2, p1 = price_high_pos[i], price_high_pos[i - 1]
        bar_dist = p2 - p1
        if not (MIN_DIV_BARS <= bar_dist <= MAX_DIV_BARS):
            continue

        price1 = df["high"].iloc[p1]
        price2 = df["high"].iloc[p2]
        if price2 >= price1:
            continue
        strength = (price1 - price2) / price1
        if strength < PRICE_STRENGTH_MIN:
            continue

        m_candidates = [m for m in macd_high_pos if p1 <= m <= p2]
        if len(m_candidates) < 2:
            continue
        m1, m2 = m_candidates[0], m_candidates[-1]
        macd1, macd2 = df["macd"].iloc[m1], df["macd"].iloc[m2]
        if macd2 <= macd1:
            continue

        recency = (n - 1) - p2
        if recency > MAX_RECENCY_BARS:
            continue

        results.append(_build_signal(
            "BEARISH HIDDEN", "🟠", df, p1, p2, m1, m2,
            price1, price2, macd1, macd2, strength, recency, bar_dist, "SELL (trend cont.)"
        ))

    results.sort(key=lambda x: x["recency_bars"])
    return results


def _build_signal(sig_type, emoji, df, p1, p2, m1, m2,
                  price1, price2, macd1, macd2,
                  strength, recency, bar_dist, bias) -> dict:
    return {
        "type": sig_type,
        "sub_type": "macd_line",
        "emoji": emoji,
        "symbol": df.attrs.get("symbol", ""),
        "timeframe": df.attrs.get("timeframe", ""),
        "bias": bias,
        "pivot1_time": str(df.index[p1]),
        "pivot2_time": str(df.index[p2]),
        "price_p1": round(float(price1), 2),
        "price_p2": round(float(price2), 2),
        "macd_p1": round(float(macd1), 4),
        "macd_p2": round(float(macd2), 4),
        "strength_pct": round(strength * 100, 2),
        "recency_bars": recency,
        "bar_dist": bar_dist,
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  RSI DIVERGENCE CHECKER
#  Called after MACD divergence is found — checks if RSI agrees on same pivots
# ═══════════════════════════════════════════════════════════════════════════════

def check_rsi_divergence(df: pd.DataFrame, signal: dict) -> bool:
    """
    Returns True if RSI also shows divergence in the same direction
    around the same price pivot region as the MACD signal.
    """
    if "rsi" not in df.columns:
        return False

    sub_type = signal.get("sub_type", "")
    bias = signal.get("bias", "")

    # Determine which price pivots to look around
    if sub_type == "histogram_divergence":
        if "BUY" in bias:
            bar1 = signal.get("low1_bar", signal.get("spike1_bar", 0))
            bar2 = signal.get("low2_bar", signal.get("spike2_bar", 0))
        else:
            bar1 = signal.get("high1_bar", signal.get("spike1_bar", 0))
            bar2 = signal.get("high2_bar", signal.get("spike2_bar", 0))
    else:
        # Find bar index from timestamp
        try:
            bar1 = df.index.get_loc(signal["pivot1_time"])
            bar2 = df.index.get_loc(signal["pivot2_time"])
        except Exception:
            return False

    window = PIVOT_LOOKBACK + 3  # slightly wider window for RSI pivots

    # Get RSI values around each pivot
    rsi1_window = df["rsi"].iloc[max(0, bar1 - window): bar1 + window + 1]
    rsi2_window = df["rsi"].iloc[max(0, bar2 - window): bar2 + window + 1]

    if rsi1_window.empty or rsi2_window.empty:
        return False

    if "BUY" in bias:
        # Bullish: RSI at pivot2 should be HIGHER than RSI at pivot1 (higher low on RSI)
        rsi1_low = rsi1_window.min()
        rsi2_low = rsi2_window.min()
        rsi_confirms = (rsi2_low > rsi1_low) and (rsi2_low < RSI_OVERSOLD + 20)
    else:
        # Bearish: RSI at pivot2 should be LOWER than RSI at pivot1 (lower high on RSI)
        rsi1_high = rsi1_window.max()
        rsi2_high = rsi2_window.max()
        rsi_confirms = (rsi2_high < rsi1_high) and (rsi2_high > RSI_OVERBOUGHT - 20)

    return rsi_confirms


# ═══════════════════════════════════════════════════════════════════════════════
#  VWAP CONFLUENCE CHECKER
#  Checks if current price is near VWAP support/resistance (band edge)
# ═══════════════════════════════════════════════════════════════════════════════

def check_vwap_confluence(df: pd.DataFrame, signal: dict) -> bool:
    """
    Returns True if current price is near VWAP lower band (bullish)
    or VWAP upper band (bearish), confirming the divergence has a
    structural support/resistance level behind it.
    """
    if "vwap" not in df.columns or df["vwap"].isna().all():
        return False

    last = df.iloc[-1]
    close = last["close"]
    vwap = last["vwap"]
    vwap_upper = last["vwap_upper"]
    vwap_lower = last["vwap_lower"]

    if pd.isna(vwap):
        return False

    band_width = vwap_upper - vwap_lower
    if band_width == 0:
        return False

    # How close is price to VWAP bands? (within 30% of band width = confluence zone)
    tolerance = band_width * 0.30
    bias = signal.get("bias", "")

    if "BUY" in bias:
        # Want price near or below VWAP lower band (oversold + divergence = strong buy)
        return close <= vwap_lower + tolerance
    else:
        # Want price near or above VWAP upper band (overbought + divergence = strong sell)
        return close >= vwap_upper - tolerance


# ═══════════════════════════════════════════════════════════════════════════════
#  CONFLUENCE SCORING
# ═══════════════════════════════════════════════════════════════════════════════

def score_signal(signal: dict, rsi_ok: bool, vwap_ok: bool) -> dict:
    """
    Attach confluence tier and confidence to each signal.

    Tier system:
      MACD only              → GOOD     ⭐⭐
      MACD + RSI             → BEST     ⭐⭐⭐
      MACD + RSI + VWAP      → SUPERB   ⭐⭐⭐⭐⭐
      MACD + VWAP (no RSI)   → GOOD+    ⭐⭐⭐  (decent, but RSI missing)
    """
    signal = signal.copy()
    signal["rsi_confirms"] = rsi_ok
    signal["vwap_confirms"] = vwap_ok

    if rsi_ok and vwap_ok:
        signal["confluence"] = "SUPERB"
        signal["stars"] = "⭐⭐⭐⭐⭐"
        signal["tier_label"] = "MACD + RSI + VWAP"
        signal["priority"] = 1
    elif rsi_ok:
        signal["confluence"] = "BEST"
        signal["stars"] = "⭐⭐⭐"
        signal["tier_label"] = "MACD + RSI"
        signal["priority"] = 2
    elif vwap_ok:
        signal["confluence"] = "GOOD+"
        signal["stars"] = "⭐⭐⭐"
        signal["tier_label"] = "MACD + VWAP"
        signal["priority"] = 3
    else:
        signal["confluence"] = "GOOD"
        signal["stars"] = "⭐⭐"
        signal["tier_label"] = "MACD only"
        signal["priority"] = 4

    # Freshness bonus
    signal["freshness"] = "FRESH 🔥" if signal["recency_bars"] <= 2 else (
        "RECENT" if signal["recency_bars"] <= 6 else "OLDER")
    return signal


# ═══════════════════════════════════════════════════════════════════════════════
#  DATA FETCHER
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_candles(instrument_token: int, tradingsymbol: str, timeframe: str) -> Optional[pd.DataFrame]:
    cfg = TIMEFRAME_MAP.get(timeframe)
    if not cfg:
        raise ValueError(f"Unknown timeframe: {timeframe}")

    to_date = datetime.now()
    from_date = to_date - timedelta(days=cfg["lookback_days"])

    try:
        data = kite.historical_data(
            instrument_token=instrument_token,
            from_date=from_date.strftime("%Y-%m-%d"),
            to_date=to_date.strftime("%Y-%m-%d"),
            interval=cfg["interval"],
            continuous=False,
            oi=False,
        )
    except Exception as e:
        log.error(f"Fetch failed — {tradingsymbol} [{timeframe}]: {e}")
        return None

    if not data:
        return None

    df = pd.DataFrame(data)
    df.rename(columns={"date": "datetime"}, inplace=True)
    df.set_index("datetime", inplace=True)
    df.attrs["symbol"] = tradingsymbol
    df.attrs["timeframe"] = timeframe
    return df


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN SCANNER
# ═══════════════════════════════════════════════════════════════════════════════

def scan_symbol(instrument: dict, timeframes: list[str]) -> list[dict]:
    """Run full scan (MACD + histogram + RSI + VWAP) on one symbol."""
    token = instrument["instrument_token"]
    symbol = instrument["tradingsymbol"]
    all_signals = []

    for tf in timeframes:
        df = fetch_candles(token, symbol, tf)
        if df is None or len(df) < MACD_SLOW + MACD_SIGNAL + 30:
            log.warning(f"  Skipping {symbol} [{tf}] — not enough data")
            continue

        df = compute_all(df)

        # ① MACD line divergences
        macd_signals = detect_macd_line_divergence(df)

        # ② MACD histogram divergences (your pattern)
        hist_signals = detect_histogram_divergence(df)

        for sig in macd_signals + hist_signals:
            rsi_ok = check_rsi_divergence(df, sig)
            vwap_ok = check_vwap_confluence(df, sig)
            scored = score_signal(sig, rsi_ok, vwap_ok)
            all_signals.append(scored)

        time.sleep(0.3)  # Kite rate limit safety

    return all_signals


def scan_all(timeframes: list[str] = None, watchlist: list[dict] = None) -> list[dict]:
    """Scan entire watchlist and return all signals sorted by priority."""
    if timeframes is None:
        timeframes = DEFAULT_TIMEFRAMES
    if watchlist is None:
        watchlist = WATCHLIST

    all_signals = []
    log.info(f"Starting scan — {len(watchlist)} symbols × {timeframes}")

    for instrument in watchlist:
        symbol = instrument["tradingsymbol"]
        log.info(f"  Scanning {symbol}…")
        signals = scan_symbol(instrument, timeframes)
        all_signals.extend(signals)
        log.info(f"  → {len(signals)} signal(s) found")

    # Sort: priority tier first, then freshness (recency)
    all_signals.sort(key=lambda x: (x["priority"], x["recency_bars"]))
    return all_signals


# ═══════════════════════════════════════════════════════════════════════════════
#  REPORT PRINTER
# ═══════════════════════════════════════════════════════════════════════════════

TIER_COLORS = {
    "SUPERB": "\033[92m",  # bright green
    "BEST": "\033[93m",  # yellow
    "GOOD+": "\033[96m",  # cyan
    "GOOD": "\033[0m",  # default
}
RESET = "\033[0m"


def print_report(signals: list[dict], filter_tier: str = None) -> None:
    """
    Print formatted report.
    filter_tier: optionally show only 'SUPERB', 'BEST', 'GOOD+', or 'GOOD'
    """
    if filter_tier:
        signals = [s for s in signals if s["confluence"] == filter_tier]

    print("\n" + "═" * 72)
    print("  DIVERGENCE SCANNER  v2  —  MULTI-LAYER CONFLUENCE REPORT")
    print("  " + datetime.now().strftime("%d %b %Y  %H:%M:%S IST"))
    print("═" * 72)

    if not signals:
        print("\n  ✅ No signals found for the selected filters.\n")
        return

    # Group by confluence tier
    tiers = ["SUPERB", "BEST", "GOOD+", "GOOD"]
    for tier in tiers:
        tier_signals = [s for s in signals if s["confluence"] == tier]
        if not tier_signals:
            continue

        color = TIER_COLORS.get(tier, "")
        print(f"\n{color}  ── {tier} ──  {tier_signals[0]['stars']}{RESET}")

        for s in tier_signals:
            print(f"\n  {s['emoji']}  {s['type']}  |  {s['symbol']}  |  {s['timeframe'].upper()}  |  {s['freshness']}")
            print(f"     Bias        : {s['bias']}")
            print(f"     Confluence  : {s['tier_label']}  {s['stars']}")
            print(f"     RSI confirms: {'YES ✓' if s['rsi_confirms'] else 'no'}")
            print(f"     VWAP conf.  : {'YES ✓' if s['vwap_confirms'] else 'no'}")
            print(f"     Recency     : {s['recency_bars']} bar(s) ago")

            if s["sub_type"] == "histogram_divergence":
                is_bull = "BUY" in s["bias"]
                p1 = s.get("price_low1" if is_bull else "price_high1", "?")
                p2 = s.get("price_low2" if is_bull else "price_high2", "?")
                direction = "lower low ↓" if is_bull else "higher high ↑"
                xover_key = "hist_crossover_low" if is_bull else "hist_crossover_high"
                xover_val = s.get(xover_key, "?")
                interp = ("Sellers pushed price lower but lost momentum → reversal UP"
                          if is_bull else
                          "Buyers pushed price higher but lost momentum → reversal DOWN")
                print(f"     Price        : {p1}  →  {p2}  ({direction}) — {s['strength_pct']}% move")
                print(
                    f"     Hist sequence: spike1={s['hist_spike1']} → crossover({xover_val}) → spike2={s['hist_spike2']}")
                print(f"     Hist weakness: 2nd spike {s['hist_weakness_pct']}% weaker than 1st  ← key signal")
                print(f"     Meaning      : {interp}")
            else:
                print(f"     Price pivots: {s['price_p1']}  →  {s['price_p2']}  ({s['strength_pct']}% move)")
                print(f"     MACD pivots : {s['macd_p1']}  →  {s['macd_p2']}")
                print(f"     Pivot 1 time: {s['pivot1_time']}")
                print(f"     Pivot 2 time: {s['pivot2_time']}")

            print("     " + "─" * 60)

    total_superb = sum(1 for s in signals if s["confluence"] == "SUPERB")
    total_best = sum(1 for s in signals if s["confluence"] == "BEST")
    print(f"\n  Total signals: {len(signals)}  |  SUPERB: {total_superb}  |  BEST: {total_best}\n")


# ═══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Divergence Scanner v2 — Kite Connect")
    parser.add_argument(
        "--timeframes", nargs="+",
        default=DEFAULT_TIMEFRAMES,
        choices=list(TIMEFRAME_MAP.keys()),
        help="Timeframes to scan"
    )
    parser.add_argument(
        "--filter", default=None,
        choices=["SUPERB", "BEST", "GOOD+", "GOOD"],
        help="Show only signals of this tier"
    )
    parser.add_argument(
        "--loop", type=int, default=0,
        help="Repeat scan every N minutes (0 = run once)"
    )
    parser.add_argument(
        "--symbols", nargs="+", default=None,
        help="Scan only these symbols (e.g. --symbols RELIANCE INFY NIFTY50)"
    )
    args = parser.parse_args()

    # Optional: restrict to specific symbols
    watchlist = WATCHLIST
    if args.symbols:
        watchlist = [w for w in WATCHLIST if w["tradingsymbol"] in args.symbols]
        if not watchlist:
            log.error(f"None of {args.symbols} found in WATCHLIST. Check symbol names.")
            exit(1)

    log.info("Divergence Scanner v2 — started")
    log.info(f"Symbols   : {[w['tradingsymbol'] for w in watchlist]}")
    log.info(f"Timeframes: {args.timeframes}")

    while True:
        signals = scan_all(timeframes=args.timeframes, watchlist=watchlist)
        print_report(signals, filter_tier=args.filter)

        if args.loop <= 0:
            break

        log.info(f"Sleeping {args.loop} min before next scan…")
        time.sleep(args.loop * 60)