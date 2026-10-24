"""Research gate for original Fixed Profit continuation entries; no orders."""
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime


@dataclass(frozen=True)
class TrendSnapshot:
    closed_at: datetime
    close: Decimal
    ema36: Decimal
    ema100: Decimal


def allows_entry(price, side, snapshot, *, observed_at, periods=(36,100), require_close=True):
    if side not in {'CE','PE'}:
        raise ValueError('Choose CE or PE')
    if observed_at.tzinfo is None or snapshot.closed_at.tzinfo is None or snapshot.closed_at>observed_at:
        raise ValueError('EMA snapshot must be closed before entry')
    if not periods or any(period not in {36,100} for period in periods):
        raise ValueError('Only 36 and 100 EMA gates are supported')
    price=Decimal(str(price))
    values=(price,snapshot.close,snapshot.ema36,snapshot.ema100)
    if not all(value.is_finite() and value>0 for value in values):
        raise ValueError('Positive finite prices and EMAs required')
    direction=1 if side=='CE' else -1
    for period in periods:
        ema=snapshot.ema36 if period==36 else snapshot.ema100
        if direction*(price-ema)<=0 or (require_close and direction*(snapshot.close-ema)<=0):
            return False
    return True
