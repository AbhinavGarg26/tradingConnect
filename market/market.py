from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")


def is_market_open(now: datetime | None = None):
    """Check market hours in IST, independent of the server's timezone."""
    now = now or datetime.now(IST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    else:
        now = now.astimezone(IST)
    # Check weekday (0 = Monday, 6 = Sunday)
    if now.weekday() >= 5:
        return False

    market_start = dtime(9, 00)
    market_end = dtime(15, 40)
    return market_start <= now.time() <= market_end
