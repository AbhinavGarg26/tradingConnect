"""Price-anchor movement tracking for live market jobs."""

from typing import Optional


def track_price_movement(
    tracking_points: dict[str, float],
    symbol_key: str,
    ltp: float,
    threshold_pct: float = 1.0,
) -> Optional[dict]:
    """Return an alert payload and reset the anchor after a threshold move."""
    tracking_ltp = tracking_points.get(symbol_key)
    if tracking_ltp is None or tracking_ltp <= 0:
        tracking_points[symbol_key] = ltp
        return None

    movement_pct = (ltp - tracking_ltp) / tracking_ltp * 100
    if abs(movement_pct) < threshold_pct:
        return None

    tracking_points[symbol_key] = ltp
    return {
        "symbol": symbol_key.split(":", 1)[-1],
        "tracking_ltp": tracking_ltp,
        "ltp": ltp,
        "movement_pct": movement_pct,
    }
