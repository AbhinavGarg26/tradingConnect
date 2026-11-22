"""Safe entry-price recovery for carried broker positions."""
from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
OVERNIGHT_STOP_START = time(9, 30)


def overnight_protection_active(now: datetime | None = None) -> bool:
    """Carried positions intentionally wait out the 09:15–09:30 volatility."""
    stamp = now or datetime.now(IST)
    stamp = stamp.replace(tzinfo=IST) if stamp.tzinfo is None else stamp.astimezone(IST)
    return stamp.time() >= OVERNIGHT_STOP_START


def carried_entry_price(position: dict) -> float | None:
    """Return a broker entry only when its carried quantity is unambiguous.

    A partially modified position must still be reconciled from executions; using
    a blended broker average for it could attach a stop to the wrong lifecycle.
    """
    quantity = int(position.get("quantity") or position.get("net_quantity") or 0)
    carried = next((position.get(key) for key in (
        "overnight_quantity", "carry_forward_quantity", "previous_day_quantity",
    ) if position.get(key) is not None), None)
    try:
        carried = int(carried)
    except (TypeError, ValueError):
        return None
    if not quantity or not carried or abs(carried) != abs(quantity):
        return None
    for key in ("overnight_average_price", "carry_forward_average_price",
                "net_price", "average_price", "buy_average_price"):
        try:
            value = float(position.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None
