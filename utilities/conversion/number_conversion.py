from decimal import Decimal, InvalidOperation
import math


def to_dec(value, default=Decimal("0")) -> Decimal:
    try:
        return Decimal(str(value)) if value is not None else default
    except InvalidOperation:
        return default


def round_to_tick(price, tick_size=0.05):
    """Rounds price down to the nearest multiple of tick_size (0.05)."""
    return round(math.floor(price / tick_size) * tick_size, 2)
