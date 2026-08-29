from datetime import datetime, time
import zoneinfo

IST = zoneinfo.ZoneInfo("Asia/Kolkata")

# ── Set to True to force market hours ON for testing ──────────
FORCE_MARKET_OPEN = True

def is_market_hours() -> bool:
    if FORCE_MARKET_OPEN:
        return True
    ist = datetime.now(IST)
    if ist.weekday() >= 5:
        return False
    return time(9, 0) <= ist.time() <= time(15, 45)

def current_ist() -> datetime:
    return datetime.now(IST)

def is_trading_day() -> bool:
    return datetime.now(IST).weekday() < 5