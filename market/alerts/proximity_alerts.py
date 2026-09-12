"""Condition building and two-alert-window rules for price proximity."""

from datetime import datetime, timedelta
from typing import Optional


ALERT_WINDOW = timedelta(days=2)
SECOND_ALERT_DELAY = timedelta(minutes=15)


def proximity_condition(
    *,
    key: str,
    label: str,
    ltp: float,
    reference: float,
    buffer_pct: float,
    zone_lower: Optional[float] = None,
    zone_upper: Optional[float] = None,
) -> Optional[dict]:
    if reference <= 0:
        return None
    lower = zone_lower if zone_lower is not None else reference * (1 - buffer_pct / 100)
    upper = zone_upper if zone_upper is not None else reference * (1 + buffer_pct / 100)
    if lower > upper:
        lower, upper = upper, lower
    if not lower <= ltp <= upper:
        return None
    return {
        "key": key,
        "label": label,
        "ltp": ltp,
        "reference": reference,
        "zone_lower": lower,
        "zone_upper": upper,
        "distance_pct": (ltp - reference) / reference * 100,
    }


def alert_is_due(now: datetime, count: int, last_alert_at: Optional[datetime]) -> bool:
    if count >= 2:
        return False
    return count == 0 or last_alert_at is None or now - last_alert_at >= SECOND_ALERT_DELAY
