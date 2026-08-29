"""
Candlestick Reversal Pattern Scanner
=====================================
Uses your existing trading.user_token login.
Scans watchlist for 1, 2, and 3-candle reversal patterns.

Usage
-----
    python candlestick_scanner.py
    python candlestick_scanner.py --interval 15minute
    python candlestick_scanner.py --interval day --signal bullish --strength strong
    python candlestick_scanner.py --symbols RELIANCE HDFCBANK INFY
    python candlestick_scanner.py --watchlist my_watchlist.txt
"""

import time
import logging
import argparse
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from dotenv import load_dotenv
load_dotenv()

from trading.user_token import fetch_user_token

try:
    from tabulate import tabulate
    HAS_TABULATE = True
except ImportError:
    HAS_TABULATE = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("scanner")

# ═══════════════════════════════════════════════════════════════════════════
# WATCHLIST
# ═══════════════════════════════════════════════════════════════════════════

DEFAULT_WATCHLIST = [
    "NIFTY 50",
    "NIFTY BANK",
    "RELIANCE",
    "HDFCBANK",
    "INFY",
    "TCS",
    "ICICIBANK",
    "SBIN",
    "AXISBANK",
    "KOTAKBANK",
    "WIPRO",
    "TATAMOTORS",
    "BAJFINANCE",
    "MARUTI",
    "LT",
    "ADANIENT",
    "ADANIPORTS",
    "NTPC",
    "POWERGRID",
    "IRCTC",
]

DEFAULT_INTERVAL   = "day"
DEFAULT_PERIOD_DAYS = 15
API_PAUSE           = 0.35   # seconds between Kite API calls

# ── Pattern thresholds ─────────────────────────────────────────────────────
DOJI_BODY_PCT     = 0.10   # body ≤ 10% of range → doji
HAMMER_WICK_RATIO = 2.0    # lower/upper wick ≥ 2× body
HARAMI_BODY_RATIO = 0.60   # harami body ≤ 60% of prior body
PIERCING_MID      = 0.50   # piercing/dark-cloud must cross 50% of prior body
MARUBOZU_WICK_PCT = 0.02   # wicks ≤ 2% of range → marubozu
STAR_BODY_RATIO   = 0.35   # star middle candle body ≤ 35% of C1 body
TWEEZER_TOLERANCE = 0.015  # shared high/low within 1.5% of range

# ═══════════════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Candle:
    open:   float
    high:   float
    low:    float
    close:  float
    volume: int
    date:   datetime = None

    body:        float = field(init=False)
    body_top:    float = field(init=False)
    body_bot:    float = field(init=False)
    upper_wick:  float = field(init=False)
    lower_wick:  float = field(init=False)
    rng:         float = field(init=False)
    is_bullish:  bool  = field(init=False)
    is_bearish:  bool  = field(init=False)
    is_doji:     bool  = field(init=False)

    def __post_init__(self):
        self.body       = abs(self.close - self.open)
        self.body_top   = max(self.open, self.close)
        self.body_bot   = min(self.open, self.close)
        self.upper_wick = self.high - self.body_top
        self.lower_wick = self.body_bot - self.low
        self.rng        = self.high - self.low
        self.is_bullish = self.close > self.open
        self.is_bearish = self.close < self.open
        self.is_doji    = (self.rng > 0) and (self.body / self.rng <= DOJI_BODY_PCT)


@dataclass
class PatternResult:
    symbol:      str
    pattern:     str
    signal:      str        # bullish | bearish | neutral
    strength:    str        # strong | moderate | weak
    candle_date: datetime
    close:       float
    action:      str
    description: str = ""


# ═══════════════════════════════════════════════════════════════════════════
# KITE HELPERS
# ═══════════════════════════════════════════════════════════════════════════

INDEX_MAP = {
    "NIFTY 50":          "NSE:NIFTY 50",
    "NIFTY":             "NSE:NIFTY 50",
    "NIFTY BANK":        "NSE:NIFTY BANK",
    "BANKNIFTY":         "NSE:NIFTY BANK",
    "FINNIFTY":          "NSE:NIFTY FIN SERVICE",
    "NIFTY MIDCAP 50":   "NSE:NIFTY MIDCAP 50",
}

_token_cache: dict[str, int] = {}


def resolve_token(kite, symbol: str) -> int:
    """Resolve NSE symbol → instrument token (cached after first call)."""
    instrument = INDEX_MAP.get(symbol.upper(), f"NSE:{symbol}")

    if instrument in _token_cache:
        return _token_cache[instrument]

    exchange, tradingsymbol = instrument.split(":", 1)
    for inst in kite.instruments(exchange):
        if inst["tradingsymbol"] == tradingsymbol:
            _token_cache[instrument] = inst["instrument_token"]
            return inst["instrument_token"]

    raise ValueError(f"Instrument not found on NSE: {symbol}")


def fetch_candles(kite, symbol: str, interval: str, period_days: int) -> list[Candle]:
    to_date   = datetime.now()
    from_date = to_date - timedelta(days=period_days)

    token = resolve_token(kite, symbol)
    rows  = kite.historical_data(token, from_date, to_date, interval)

    candles = []
    for r in rows:
        if not r["high"]:
            continue
        candles.append(Candle(
            open   = float(r["open"]),
            high   = float(r["high"]),
            low    = float(r["low"]),
            close  = float(r["close"]),
            volume = int(r["volume"]),
            date   = r["date"],
        ))
    return candles


# ═══════════════════════════════════════════════════════════════════════════
# PATTERN ENGINE
# ═══════════════════════════════════════════════════════════════════════════

def _pct(n, d):
    return 0.0 if d == 0 else n / d


# ── Single candle ──────────────────────────────────────────────────────────

def scan_single(c: Candle) -> list[dict]:
    r, bw, rng = [], c.body, c.rng
    if rng == 0:
        return r

    # Hammer
    if c.lower_wick >= bw * HAMMER_WICK_RATIO and c.upper_wick <= bw * 0.5 and bw > 0 and _pct(bw, rng) >= 0.15:
        r.append(dict(pattern="Hammer", signal="bullish", strength="strong",
            action="Buy above high. SL below low.",
            description="Long lower wick ≥2× body. Buyers absorbed all selling pressure."))

    # Hanging Man
    if c.lower_wick >= bw * HAMMER_WICK_RATIO and c.upper_wick <= bw * 0.5 and bw > 0 and c.is_bearish:
        r.append(dict(pattern="Hanging Man", signal="bearish", strength="moderate",
            action="Confirm with next bearish candle. SL above high.",
            description="Hammer shape with red body — distribution warning in uptrend."))

    # Inverted Hammer
    if c.upper_wick >= bw * HAMMER_WICK_RATIO and c.lower_wick <= bw * 0.5 and bw > 0 and c.is_bullish:
        r.append(dict(pattern="Inverted Hammer", signal="bullish", strength="moderate",
            action="Enter only on next bullish confirmation. SL below low.",
            description="Long upper wick in downtrend. Buyers attempted rally — needs confirmation."))

    # Shooting Star
    if c.upper_wick >= bw * HAMMER_WICK_RATIO and c.lower_wick <= bw * 0.5 and bw > 0 and c.is_bearish:
        r.append(dict(pattern="Shooting Star", signal="bearish", strength="strong",
            action="Short below low. SL above high.",
            description="Long upper wick, bearish close. Buyers failed to hold gains."))

    # Doji family
    if c.is_doji:
        if c.lower_wick > c.upper_wick * 3:
            r.append(dict(pattern="Dragonfly Doji", signal="bullish", strength="moderate",
                action="Buy on next bullish candle. SL below doji low.",
                description="Open=close at top, long lower wick. Buyers absorbed all selling."))
        elif c.upper_wick > c.lower_wick * 3:
            r.append(dict(pattern="Gravestone Doji", signal="bearish", strength="moderate",
                action="Short on next bearish candle. SL above doji high.",
                description="Open=close at bottom, long upper wick. Bulls failed to hold highs."))
        else:
            r.append(dict(pattern="Standard Doji", signal="neutral", strength="weak",
                action="Wait for next candle direction before trading.",
                description="Open ≈ Close. Full market indecision."))

    # Marubozu
    if c.upper_wick <= rng * MARUBOZU_WICK_PCT and c.lower_wick <= rng * MARUBOZU_WICK_PCT and _pct(bw, rng) >= 0.95:
        label = "Bullish Marubozu" if c.is_bullish else "Bearish Marubozu"
        r.append(dict(pattern=label, signal="bullish" if c.is_bullish else "bearish", strength="strong",
            action=("Enter on pullback to body midpoint." if c.is_bullish else "Short on any bounce."),
            description=("No wicks — complete buyer dominance." if c.is_bullish else "No wicks — complete seller dominance.")))

    # Spinning Top
    if not c.is_doji and bw <= rng * 0.3 and c.upper_wick >= rng * 0.2 and c.lower_wick >= rng * 0.2:
        r.append(dict(pattern="Spinning Top", signal="neutral", strength="weak",
            action="Watch for directional candle with volume.",
            description="Small body, long wicks both sides — exhaustion/indecision."))

    return r


# ── Double candle ──────────────────────────────────────────────────────────

def scan_double(c1: Candle, c2: Candle) -> list[dict]:
    r = []

    # Bullish Engulfing
    if c1.is_bearish and c2.is_bullish and c2.open < c1.close and c2.close > c1.open and c2.body > c1.body:
        r.append(dict(pattern="Bullish Engulfing", signal="bullish", strength="strong",
            action="Buy above C2 close. SL below C2 low.",
            description="C2 opens below C1 close, closes above C1 open — complete buyer takeover."))

    # Bearish Engulfing
    if c1.is_bullish and c2.is_bearish and c2.open > c1.close and c2.close < c1.open and c2.body > c1.body:
        r.append(dict(pattern="Bearish Engulfing", signal="bearish", strength="strong",
            action="Short below C2 close. SL above C2 high.",
            description="C2 opens above C1 close, closes below C1 open — complete seller takeover."))

    # Piercing Pattern
    if c1.is_bearish and c2.is_bullish and c2.open < c1.low and c2.close > c1.body_bot + c1.body * PIERCING_MID and c2.close < c1.open:
        r.append(dict(pattern="Piercing Pattern", signal="bullish", strength="strong",
            action="Buy above C2 high. SL below C2 low.",
            description="C2 gaps below C1 low, recovers above C1 body midpoint — strong floor."))

    # Dark Cloud Cover
    if c1.is_bullish and c2.is_bearish and c2.open > c1.high and c2.close < c1.body_top - c1.body * PIERCING_MID and c2.close > c1.open:
        r.append(dict(pattern="Dark Cloud Cover", signal="bearish", strength="strong",
            action="Short below C2 low. SL above C2 open.",
            description="C2 gaps above C1 high, closes below C1 body midpoint — classic distribution."))

    # Bullish Harami
    if c1.is_bearish and c2.is_bullish and c2.open > c1.close and c2.close < c1.open and c2.body < c1.body * HARAMI_BODY_RATIO:
        r.append(dict(pattern="Bullish Harami", signal="bullish", strength="moderate",
            action="Wait for third bullish candle. SL below C1 low.",
            description="Small bullish C2 inside large bearish C1 body. Selling momentum slowing."))

    # Bearish Harami
    if c1.is_bullish and c2.is_bearish and c2.open < c1.close and c2.close > c1.open and c2.body < c1.body * HARAMI_BODY_RATIO:
        r.append(dict(pattern="Bearish Harami", signal="bearish", strength="moderate",
            action="Wait for third bearish candle. SL above C1 high.",
            description="Small bearish C2 inside large bullish C1 body. Buying momentum stalling."))

    # Harami Cross
    if c1.body > 0 and c2.is_doji and c2.body_top <= c1.body_top and c2.body_bot >= c1.body_bot:
        sig = "bullish" if c1.is_bearish else "bearish"
        r.append(dict(pattern=f"Harami Cross ({sig.capitalize()})", signal=sig, strength="strong",
            action=("Buy on next bullish candle." if sig == "bullish" else "Short on next bearish candle."),
            description="Doji inside prior candle body — high-conviction indecision after trend move."))

    # Tweezer Top
    if c1.is_bullish and c2.is_bearish and abs(c1.high - c2.high) <= c1.rng * TWEEZER_TOLERANCE:
        r.append(dict(pattern="Tweezer Top", signal="bearish", strength="moderate",
            action="Short on break below C2 low. SL above shared high.",
            description="Same high tested and rejected twice — double resistance confirmation."))

    # Tweezer Bottom
    if c1.is_bearish and c2.is_bullish and abs(c1.low - c2.low) <= c1.rng * TWEEZER_TOLERANCE:
        r.append(dict(pattern="Tweezer Bottom", signal="bullish", strength="moderate",
            action="Buy above C2 high. SL below shared low.",
            description="Same low tested and held twice — double support confirmation."))

    # On-Neck
    if c1.is_bearish and c2.is_bullish and c1.low * 0.995 <= c2.close <= c1.low * 1.005:
        r.append(dict(pattern="On-Neck Line", signal="bearish", strength="moderate",
            action="Downtrend continuation. Exit longs or short C2 close.",
            description="C2 only recovers to C1 low — bulls trapped. Bearish continuation."))

    return r


# ── Triple candle ──────────────────────────────────────────────────────────

def scan_triple(c1: Candle, c2: Candle, c3: Candle) -> list[dict]:
    r = []

    # Morning Star / Morning Doji Star
    if (c1.is_bearish and c1.body > c1.rng * 0.4
            and c2.body <= c1.body * STAR_BODY_RATIO
            and c3.is_bullish and c3.close >= c1.body_bot + c1.body * 0.5):
        name = "Morning Doji Star" if c2.is_doji else "Morning Star"
        r.append(dict(pattern=name, signal="bullish", strength="strong",
            action="Buy above C3 close. SL below C2 low.",
            description=("Doji" if c2.is_doji else "Small candle") + " between bearish C1 and bullish C3 recovering ≥50% of C1 body."))

    # Evening Star / Evening Doji Star
    if (c1.is_bullish and c1.body > c1.rng * 0.4
            and c2.body <= c1.body * STAR_BODY_RATIO
            and c3.is_bearish and c3.close <= c1.body_top - c1.body * 0.5):
        name = "Evening Doji Star" if c2.is_doji else "Evening Star"
        r.append(dict(pattern=name, signal="bearish", strength="strong",
            action="Short below C3 close. SL above C2 high.",
            description=("Doji" if c2.is_doji else "Small candle") + " between bullish C1 and bearish C3 erasing ≥50% of C1 body."))

    # Three White Soldiers
    if (c1.is_bullish and c2.is_bullish and c3.is_bullish
            and c1.open < c2.open < c1.close < c2.close
            and c2.open < c3.open < c2.close < c3.close
            and c1.body > c1.rng * 0.5 and c2.body > c2.rng * 0.5 and c3.body > c3.rng * 0.5):
        r.append(dict(pattern="Three White Soldiers", signal="bullish", strength="strong",
            action="Buy on pullback to C3 body midpoint. Tight SL below C3 low.",
            description="Three consecutive strong bullish candles, each opening within prior body."))

    # Three Black Crows
    if (c1.is_bearish and c2.is_bearish and c3.is_bearish
            and c1.close < c2.open < c1.open
            and c2.close < c3.open < c2.open
            and c1.body > c1.rng * 0.5 and c2.body > c2.rng * 0.5 and c3.body > c3.rng * 0.5):
        r.append(dict(pattern="Three Black Crows", signal="bearish", strength="strong",
            action="Short on any bounce. SL above C3 open.",
            description="Three consecutive strong bearish candles, each opening within prior body."))

    # Three Inside Up
    if (c1.is_bearish and c2.is_bullish
            and c1.close < c2.open < c2.close < c1.open
            and c2.body < c1.body * HARAMI_BODY_RATIO
            and c3.is_bullish and c3.close > c1.open):
        r.append(dict(pattern="Three Inside Up", signal="bullish", strength="strong",
            action="Buy above C3 close. SL below C2 low.",
            description="Bullish harami (C1+C2) confirmed by C3 closing above C1 open."))

    # Three Inside Down
    if (c1.is_bullish and c2.is_bearish
            and c1.open < c2.close < c2.open < c1.close
            and c2.body < c1.body * HARAMI_BODY_RATIO
            and c3.is_bearish and c3.close < c1.open):
        r.append(dict(pattern="Three Inside Down", signal="bearish", strength="strong",
            action="Short below C3 close. SL above C2 high.",
            description="Bearish harami (C1+C2) confirmed by C3 closing below C1 open."))

    # Abandoned Baby Bullish
    if c1.is_bearish and c2.is_doji and c2.high < c1.low and c3.is_bullish and c3.low > c2.high:
        r.append(dict(pattern="Abandoned Baby (Bullish)", signal="bullish", strength="strong",
            action="Aggressive buy above C3. SL below C2 low.",
            description="Doji gaps completely below C1 and above C3 — isolated island bottom."))

    # Abandoned Baby Bearish
    if c1.is_bullish and c2.is_doji and c2.low > c1.high and c3.is_bearish and c3.high < c2.low:
        r.append(dict(pattern="Abandoned Baby (Bearish)", signal="bearish", strength="strong",
            action="Aggressive short below C3. SL above C2 high.",
            description="Doji gaps completely above C1 and below C3 — isolated island top."))

    # Deliberation
    if (c1.is_bullish and c2.is_bullish and c3.is_bullish
            and c1.body > c1.rng * 0.5 and c2.body > c2.rng * 0.5
            and c3.body < c1.body * 0.5 and c3.upper_wick > c3.body):
        r.append(dict(pattern="Deliberation Pattern", signal="bearish", strength="moderate",
            action="Tighten trailing SL on longs. Watch for reversal.",
            description="Two strong bullish candles then weak third with upper wick — buying momentum fading."))

    return r


# ═══════════════════════════════════════════════════════════════════════════
# SCANNER ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════════════════

def scan_symbol(kite, symbol: str, interval: str, period_days: int) -> list[PatternResult]:
    candles = fetch_candles(kite, symbol, interval, period_days)
    if len(candles) < 3:
        log.debug("%-14s  skipped — only %d candles", symbol, len(candles))
        return []

    c1, c2, c3 = candles[-3], candles[-2], candles[-1]
    found = []

    for p in scan_single(c3):
        found.append(PatternResult(symbol=symbol, candle_date=c3.date, close=c3.close, **p))
    for p in scan_double(c2, c3):
        found.append(PatternResult(symbol=symbol, candle_date=c3.date, close=c3.close, **p))
    for p in scan_triple(c1, c2, c3):
        found.append(PatternResult(symbol=symbol, candle_date=c3.date, close=c3.close, **p))

    return found


def run_scan(
    symbols:         list[str],
    interval:        str = DEFAULT_INTERVAL,
    period_days:     int = DEFAULT_PERIOD_DAYS,
    signal_filter:   Optional[str] = None,
    strength_filter: Optional[str] = None,
) -> list[PatternResult]:

    kite, user_id = fetch_user_token(log)
    log.info("Logged in as %s", user_id)
    log.info("Scanning %d symbols  |  interval=%s  |  lookback=%dd",
             len(symbols), interval, period_days)

    all_results: list[PatternResult] = []

    for i, symbol in enumerate(symbols, 1):
        log.info("[%d/%d]  %-16s", i, len(symbols), symbol)
        try:
            results = scan_symbol(kite, symbol, interval, period_days)
            if signal_filter:
                results = [r for r in results if r.signal == signal_filter]
            if strength_filter:
                results = [r for r in results if r.strength == strength_filter]
            all_results.extend(results)
        except Exception as exc:
            log.warning("%-14s  error: %s", symbol, exc)
        time.sleep(API_PAUSE)

    return all_results


# ═══════════════════════════════════════════════════════════════════════════
# OUTPUT
# ═══════════════════════════════════════════════════════════════════════════

SIGNAL_ICON  = {"bullish": "▲", "bearish": "▼", "neutral": "–"}
STRENGTH_BAR = {"strong": "●●●", "moderate": "●●○", "weak": "●○○"}


def print_results(results: list[PatternResult]) -> None:
    if not results:
        print("\n  No patterns matched.\n")
        return

    # sort: strong first, then bullish before bearish
    order = {"strong": 0, "moderate": 1, "weak": 2}
    results = sorted(results, key=lambda x: (order[x.strength], x.signal))

    rows = []
    for r in results:
        dt = r.candle_date.strftime("%d-%b %H:%M") if r.candle_date else "—"
        rows.append([
            r.symbol,
            r.pattern,
            SIGNAL_ICON[r.signal] + " " + r.signal.upper(),
            STRENGTH_BAR[r.strength],
            f"{r.close:>10,.2f}",
            dt,
            r.action,
        ])

    headers = ["Symbol", "Pattern", "Signal", "Strength", "Close", "Date/Time", "Action"]
    sep = "═" * 110

    print(f"\n{sep}")
    print(f"  CANDLESTICK SCAN  —  {len(results)} pattern(s)  —  {datetime.now():%d-%b-%Y %H:%M:%S}")
    print(sep)
    if HAS_TABULATE:
        print(tabulate(rows, headers=headers, tablefmt="simple", colalign=("left",)*7))
    else:
        widths = [14, 30, 16, 10, 12, 14, 50]
        print("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
        print("─" * 110)
        for row in rows:
            print("  ".join(str(v).ljust(widths[i]) for i, v in enumerate(row)))
    print(f"{sep}\n")


def save_csv(results: list[PatternResult], path: str = "scan_results.csv") -> None:
    if not results:
        return
    pd.DataFrame([{
        "symbol":      r.symbol,
        "pattern":     r.pattern,
        "signal":      r.signal,
        "strength":    r.strength,
        "close":       r.close,
        "candle_date": r.candle_date,
        "action":      r.action,
        "description": r.description,
    } for r in results]).to_csv(path, index=False)
    log.info("Saved → %s", path)


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

def load_watchlist_file(path: str) -> list[str]:
    with open(path) as f:
        return [l.strip().upper() for l in f if l.strip() and not l.startswith("#")]


def parse_args():
    p = argparse.ArgumentParser(description="Kite candlestick scanner")
    p.add_argument("--interval",  default=DEFAULT_INTERVAL,
                   help="Candle interval: minute, 3minute, 5minute, 10minute, 15minute, 30minute, 60minute, day")
    p.add_argument("--period",    type=int, default=DEFAULT_PERIOD_DAYS,
                   help="Lookback in calendar days (default 15)")
    p.add_argument("--signal",    choices=["bullish", "bearish"], default=None)
    p.add_argument("--strength",  choices=["strong", "moderate", "weak"], default=None)
    p.add_argument("--watchlist", default=None, help="Path to .txt watchlist file")
    p.add_argument("--symbols",   nargs="*",    help="Space-separated symbols (overrides watchlist)")
    p.add_argument("--csv",       default="scan_results.csv", help="Output CSV path")
    return p.parse_args()


if __name__ == "__main__":
    args    = parse_args()
    symbols = (
        [s.upper() for s in args.symbols] if args.symbols
        else load_watchlist_file(args.watchlist) if args.watchlist
        else DEFAULT_WATCHLIST
    )

    results = run_scan(
        symbols         = symbols,
        interval        = args.interval,
        period_days     = args.period,
        signal_filter   = args.signal,
        strength_filter = args.strength,
    )

    print_results(results)
    save_csv(results, args.csv)