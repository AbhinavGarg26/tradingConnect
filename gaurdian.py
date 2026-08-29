
"""
SL Guardian — ATR-based Stop Loss Watchdog for Kite Connect
============================================================
Runs every 60 seconds. For every open position (MIS + CNC/NRML),
checks if a live SL/SL-M order OR an active GTT order exists.
If neither is found, computes an ATR-based SL level and places
the appropriate order type automatically:

  • Equities / Futures  → SL-M order (guaranteed fill)
  • Stock Options (NFO) → SL order with market protection
    (Kite blocks SL-M on illiquid options; SL-limit with a
     small buffer below trigger is the correct workaround)

GTT check: if any active GTT exists for the symbol, the position
is considered protected and no additional order is placed.

Dependencies:
    pip install kiteconnect pandas numpy

Usage:
    1. Fill in KITE_API_KEY, KITE_ACCESS_TOKEN below (or via env vars).
    2. Customise SYMBOL_CONFIG for each instrument you trade.
    3. Run: python sl_guardian.py
"""

import os
import time
import logging
import traceback
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from kiteconnect import KiteConnect

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

KITE_API_KEY     = os.getenv("KITE_API_KEY", "YOUR_API_KEY")
KITE_ACCESS_TOKEN = os.getenv("KITE_ACCESS_TOKEN", "YOUR_ACCESS_TOKEN")

WATCHDOG_INTERVAL_SEC = 60   # how often the loop runs

# Per-symbol ATR config
# Key   : trading symbol exactly as it appears in Kite positions (e.g. "RELIANCE", "NIFTY24DECFUT")
# atr_period     : lookback candles for ATR calculation
# atr_multiplier : SL distance = atr_multiplier * ATR
# candle_interval: interval string accepted by kite.historical_data
#                  "minute", "3minute", "5minute", "15minute", "30minute", "60minute", "day"
SYMBOL_CONFIG: dict[str, dict] = {
    "RELIANCE": {
        "atr_period":     14,
        "atr_multiplier": 1.5,
        "candle_interval": "15minute",
        "exchange":       "NSE",
    },
    "NIFTY24DECFUT": {
        "atr_period":     14,
        "atr_multiplier": 2.0,
        "candle_interval": "5minute",
        "exchange":       "NFO",
    },
    # ── Add more symbols here ──────────────────
    # "INFY": {
    #     "atr_period": 7,
    #     "atr_multiplier": 1.5,
    #     "candle_interval": "15minute",
    #     "exchange": "NSE",
    # },
}

# Default fallback config if a symbol isn't in SYMBOL_CONFIG
DEFAULT_CONFIG = {
    "atr_period":     14,
    "atr_multiplier": 1.5,
    "candle_interval": "15minute",
}

# ── Exclude list ───────────────────────────────────────────────────────────────
# Symbols listed here are completely skipped by the watchdog — no SL will ever
# be placed for them. Useful for spreads, hedges, or positions you manage manually.
# Add the exact tradingsymbol as it appears in Kite positions.
EXCLUDE_SYMBOLS: set[str] = {
    # "NIFTY24DEC24500CE",   # example: manually managed hedge leg
    # "BANKEX26MARFUT",      # example: always has GTT, skip watchdog
}

# For stock options: SL-M is blocked by Kite. We place an SL (limit) order instead.
# The limit price = trigger_price * (1 - OPTIONS_MARKET_PROTECTION_PCT) for SELL SL
#                 = trigger_price * (1 + OPTIONS_MARKET_PROTECTION_PCT) for BUY  SL
# 2% buffer is a safe default — wide enough to fill, tight enough to matter.
OPTIONS_MARKET_PROTECTION_PCT = 0.02   # 2%


# ─────────────────────────────────────────────
# INSTRUMENT TYPE HELPERS
# ─────────────────────────────────────────────

def is_stock_option(position: dict) -> bool:
    """
    Detect stock options (NFO CE/PE) which require SL-limit instead of SL-M.
    Kite blocks SL-M on stock options due to illiquidity.
    Index options (NIFTY/BANKNIFTY/FINNIFTY/MIDCPNIFTY CE/PE) are also
    handled the same way for safety.
    """
    sym = position["tradingsymbol"]
    exchange = position["exchange"]

    # NFO options end with CE or PE
    if exchange == "NFO" and (sym.endswith("CE") or sym.endswith("PE")):
        return True

    # BFO (BSE F&O) options
    if exchange == "BFO" and (sym.endswith("CE") or sym.endswith("PE")):
        return True

    return False

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("sl_guardian.log"),
    ],
)
log = logging.getLogger("sl_guardian")

# ─────────────────────────────────────────────
# KITE CLIENT
# ─────────────────────────────────────────────

kite = KiteConnect(api_key="b3s8bgvlucb53nh5")
print(kite.login_url())
access_token = "at53p7YBjLMbR6w50KlcUaxxe4dFUx8W"
data = kite.generate_session(access_token, api_secret="1b2sfmed99u1e2t0equv40khmud5ny74")
kite.set_access_token(data["access_token"])


# ─────────────────────────────────────────────
# ATR CALCULATION
# ─────────────────────────────────────────────

def fetch_candles(instrument_token: int, interval: str, period: int) -> pd.DataFrame:
    """
    Fetch enough historical candles to compute ATR.
    Fetches period*3 candles as buffer for weekends/holidays.
    """
    # For intraday candles go back ~5 trading days; for daily go back period*2 calendar days
    if interval == "day":
        from_dt = datetime.now() - timedelta(days=period * 2 + 10)
    else:
        from_dt = datetime.now() - timedelta(days=5)

    to_dt = datetime.now()

    data = kite.historical_data(
        instrument_token=instrument_token,
        from_date=from_dt.strftime("%Y-%m-%d %H:%M:%S"),
        to_date=to_dt.strftime("%Y-%m-%d %H:%M:%S"),
        interval=interval,
        continuous=False,
        oi=False,
    )
    if not data:
        raise ValueError("No historical data returned")

    df = pd.DataFrame(data)
    df.set_index("date", inplace=True)
    return df


def compute_atr(df: pd.DataFrame, period: int) -> float:
    """
    Compute ATR using Wilder's smoothing (same as TradingView default).
    Returns the most recent ATR value.
    """
    high  = df["high"]
    low   = df["low"]
    close = df["close"]

    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)

    # Wilder's smoothing (equivalent to EMA with alpha=1/period)
    atr = tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    return float(atr.iloc[-1])


def get_atr_for_symbol(symbol: str, instrument_token: int) -> float:
    cfg      = SYMBOL_CONFIG.get(symbol, DEFAULT_CONFIG)
    period   = cfg["atr_period"]
    interval = cfg["candle_interval"]

    df  = fetch_candles(instrument_token, interval, period)
    atr = compute_atr(df, period)
    log.info(f"  ATR({period}, {interval}) for {symbol} = {atr:.4f}")
    return atr


# ─────────────────────────────────────────────
# SL DETECTION
# ─────────────────────────────────────────────

def get_open_sl_orders() -> dict[str, list]:
    """
    Returns a dict: { tradingsymbol -> [list of open SL/SL-M orders] }
    Covers both SL and SL-M order types across all OPEN + TRIGGER PENDING orders.
    """
    orders = kite.orders()
    sl_map: dict[str, list] = {}

    for o in orders:
        if o["status"] not in ("TRIGGER PENDING", "OPEN"):
            continue
        if o["order_type"] not in ("SL", "SL-M"):
            continue

        sym = o["tradingsymbol"]
        sl_map.setdefault(sym, []).append(o)

    return sl_map


def get_active_gtt_symbols() -> set[str]:
    """
    Returns a set of tradingsymbols that have at least one ACTIVE GTT order.
    GTT statuses considered 'active': active, triggered (awaiting execution).
    A position covered by a GTT is considered protected — no additional SL needed.
    """
    protected: set[str] = set()   # was wrongly initialised as {} (dict) — fixed
    try:
        gtts = kite.get_gtts()
        for gtt in gtts:
            if gtt.get("status") in ("active"):
                # GTT condition contains the tradingsymbol
                sym = gtt.get("condition", {}).get("tradingsymbol")
                if sym:
                    protected.add(sym)
    except Exception as e:
        log.warning(f"Could not fetch GTT orders (will ignore GTT check): {e}")
    return protected


def get_instrument_token(symbol: str, exchange: str) -> int:
    """Lookup instrument token. Cached in a module-level dict to avoid repeated API calls."""
    key = f"{exchange}:{symbol}"
    if key not in _token_cache:
        instruments = kite.ltp([key])
        _token_cache[key] = instruments[key]["instrument_token"]
    return _token_cache[key]

_token_cache: dict[str, int] = {}


# ─────────────────────────────────────────────
# SL PLACEMENT
# ─────────────────────────────────────────────

def place_sl_order(position: dict, sl_price: float) -> str:
    """
    Place the correct SL order type based on instrument:

      • Equities / Futures  → ORDER_TYPE_SLM  (trigger only, no limit price)
      • Stock / Index Options (NFO/BFO CE/PE)
                            → ORDER_TYPE_SL   (trigger + limit with market protection)
                              Kite blocks SL-M on options; SL-limit is the workaround.

    Returns the Kite order_id on success.
    """
    symbol    = position["tradingsymbol"]
    exchange  = position["exchange"]
    quantity  = abs(position["quantity"])
    direction = position["quantity"]   # positive = long, negative = short

    transaction_type = kite.TRANSACTION_TYPE_SELL if direction > 0 else kite.TRANSACTION_TYPE_BUY

    product = kite.PRODUCT_MIS if position["product"] == "MIS" else kite.PRODUCT_CNC
    if position["product"] == "NRML":
        product = kite.PRODUCT_NRML

    trigger = round(sl_price, 2)

    if is_stock_option(position):
        # ── Options: SL-limit with market protection ──────────────────────────
        # SELL SL: limit price slightly BELOW trigger (we accept fills down to this)
        # BUY  SL: limit price slightly ABOVE trigger (we accept fills up  to this)
        buffer = trigger * OPTIONS_MARKET_PROTECTION_PCT
        if transaction_type == kite.TRANSACTION_TYPE_SELL:
            limit_price = round(trigger - buffer, 2)
            limit_price = max(limit_price, 0.05)
        else:
            limit_price = round(trigger + buffer, 2)

        log.info(f"  Options detected → SL-limit | trigger={trigger} | limit={limit_price} "
                 f"(protection={OPTIONS_MARKET_PROTECTION_PCT*100:.1f}%)")

        order_id = kite.place_order(
            tradingsymbol    = symbol,
            exchange         = exchange,
            transaction_type = transaction_type,
            quantity         = quantity,
            order_type       = kite.ORDER_TYPE_SL,
            product          = product,
            trigger_price    = trigger,
            price            = limit_price,
            variety          = kite.VARIETY_REGULAR,
            tag              = "sl_guardian",
        )
    else:
        # ── Equities / Futures: SL-M (guaranteed fill) ───────────────────────
        log.info(f"  Equity/Futures → SL-M | trigger={trigger}")

        order_id = kite.place_order(
            tradingsymbol    = symbol,
            exchange         = exchange,
            transaction_type = transaction_type,
            quantity         = quantity,
            order_type       = kite.ORDER_TYPE_SLM,
            product          = product,
            trigger_price    = trigger,
            variety          = kite.VARIETY_REGULAR,
            tag              = "sl_guardian",
        )

    return order_id


def compute_sl_price(position: dict, atr: float) -> float:
    """
    SL price = avg_price ± (multiplier * ATR)
    Long  position: SL is BELOW avg entry
    Short position: SL is ABOVE avg entry

    Safety check: SL trigger must be strictly below LTP (SELL SL) or above LTP (BUY SL).
    If ATR-based SL violates this (e.g. option already moved far against you),
    we fall back to LTP × 0.98 for SELL or LTP × 1.02 for BUY so the order
    is always accepted by Kite.
    """
    cfg        = SYMBOL_CONFIG.get(position["tradingsymbol"], DEFAULT_CONFIG)
    multiplier = cfg["atr_multiplier"]
    avg_price  = position["average_price"]
    direction  = position["quantity"]
    symbol     = position["tradingsymbol"]
    exchange   = cfg.get("exchange", position["exchange"])

    sl_distance = multiplier * atr

    if direction > 0:   # Long → SELL SL must be below LTP
        sl_price = avg_price - sl_distance
    else:               # Short → BUY SL must be above LTP
        sl_price = avg_price + sl_distance

    sl_price = max(sl_price, 0.05)

    # ── LTP guard: validate trigger against current market price ──────────────
    try:
        ltp_data = kite.ltp([f"{exchange}:{symbol}"])
        ltp = ltp_data[f"{exchange}:{symbol}"]["last_price"]

        if direction > 0 and sl_price >= ltp:
            # ATR SL ended up above/at LTP — clamp to 2% below LTP
            old = sl_price
            sl_price = round(ltp * 0.98, 2)
            log.warning(f"  SL {old:.2f} >= LTP {ltp:.2f} for SELL — clamped to {sl_price:.2f} (LTP×0.98)")

        elif direction < 0 and sl_price <= ltp:
            # ATR SL ended up below/at LTP — clamp to 2% above LTP
            old = sl_price
            sl_price = round(ltp * 1.02, 2)
            log.warning(f"  SL {old:.2f} <= LTP {ltp:.2f} for BUY  — clamped to {sl_price:.2f} (LTP×1.02)")

    except Exception as e:
        log.warning(f"  Could not fetch LTP for {symbol} to validate SL ({e}). Using ATR value as-is.")

    return sl_price


# ─────────────────────────────────────────────
# MAIN WATCHDOG LOOP
# ─────────────────────────────────────────────

def run_watchdog_cycle():
    log.info("── Watchdog cycle starting ──────────────────────")

    # 1. Get all positions (net view includes both MIS and CNC/NRML)
    positions_raw = kite.positions()
    positions = positions_raw.get("net", [])

    # 2. Filter to only open positions (non-zero quantity)
    open_positions = [p for p in positions if p["quantity"] != 0]

    if not open_positions:
        log.info("No open positions found. Nothing to guard.")
        return

    log.info(f"Open positions: {[p['tradingsymbol'] for p in open_positions]}")

    # 3. Get current SL/SL-M orders AND active GTT orders
    sl_orders    = get_open_sl_orders()
    gtt_symbols  = get_active_gtt_symbols()

    if gtt_symbols:
        log.info(f"Active GTT protection found for: {gtt_symbols}")

    # 4. Check each position
    for pos in open_positions:
        symbol = pos["tradingsymbol"]
        log.info(f"Checking {symbol} | qty={pos['quantity']} | avg={pos['average_price']:.2f} "
                 f"| product={pos['product']} | {'OPTION' if is_stock_option(pos) else 'EQ/FUT'}")

        # ── Check 0: symbol in exclude list ───────────────────────────────────
        if symbol in EXCLUDE_SYMBOLS:
            log.info(f"  ⊘ {symbol} is in EXCLUDE_SYMBOLS. Skipping.")
            continue

        # ── Check 1: live SL/SL-M order exists ────────────────────────────────
        existing_sl = sl_orders.get(symbol, [])
        if existing_sl:
            log.info(f"  ✓ SL order present ({len(existing_sl)} order(s)). Skipping.")
            continue

        # ── Check 2: active GTT exists ────────────────────────────────────────
        if symbol in gtt_symbols:
            log.info(f"  ✓ Active GTT found for {symbol}. Skipping.")
            continue

        # ── No protection found — place ATR-based SL ──────────────────────────
        log.warning(f"  ✗ NO SL or GTT found for {symbol}. Placing ATR-based SL order...")

        try:
            cfg      = SYMBOL_CONFIG.get(symbol, DEFAULT_CONFIG)
            exchange = cfg.get("exchange", pos["exchange"])
            token    = get_instrument_token(symbol, exchange)
            atr      = get_atr_for_symbol(symbol, token)
            sl_price = compute_sl_price(pos, atr)

            log.info(f"  Computed SL price = {sl_price:.2f}  "
                     f"(ATR={atr:.4f} × {cfg.get('atr_multiplier', DEFAULT_CONFIG['atr_multiplier'])})")

            order_id = place_sl_order(pos, sl_price)
            log.info(f"  ✓ SL order placed successfully. order_id={order_id}")

        except Exception as e:
            log.error(f"  ✗ Failed to place SL for {symbol}: {e}")
            log.debug(traceback.format_exc())

    log.info("── Watchdog cycle complete ──────────────────────\n")


def main():
    log.info("=" * 55)
    log.info("SL Guardian started")
    log.info(f"Watchdog interval : {WATCHDOG_INTERVAL_SEC}s")
    log.info(f"Symbols configured: {list(SYMBOL_CONFIG.keys()) or 'Using defaults'}")
    log.info("=" * 55 + "\n")

    while True:
        try:
            run_watchdog_cycle()
        except Exception as e:
            log.error(f"Unhandled error in watchdog cycle: {e}")
            log.debug(traceback.format_exc())

        time.sleep(WATCHDOG_INTERVAL_SEC)


if __name__ == "__main__":
    main()