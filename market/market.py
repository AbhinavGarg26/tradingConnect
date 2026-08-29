from datetime import datetime, time as dtime


def is_market_open():
    """Checks if the current time is within Indian market hours (9:15 AM - 3:30 PM, Mon-Fri)."""
    now = datetime.now()
    # Check weekday (0 = Monday, 6 = Sunday)
    if now.weekday() >= 5:
        return False

    market_start = dtime(9, 15)
    market_end = dtime(15, 40)
    return market_start <= now.time() <= market_end