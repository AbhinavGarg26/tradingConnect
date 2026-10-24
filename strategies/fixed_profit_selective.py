"""Research-only pre-order filter for Fixed Profit's unchanged entry rules.

Not wired into the baseline or live execution. Feed all completed 5m candles
from 09:15 in the current session. Apply only after the original entry qualifies.
In the saved-cohort study a failed filter skips the whole day, with no later entry.
"""
from dataclasses import dataclass
from datetime import time, timedelta
from decimal import Decimal

from strategies.nifty_opening_range import IST


@dataclass(frozen=True)
class FilterDecision:
    allowed: bool
    reason: str
    atr: Decimal | None = None
    crossings: int | None = None


def evaluate_entry(candles, reference_level, *, observed_at):
    """SMA ATR14 <=20 and <=2 close crossings since 09:45; no future data."""
    if observed_at.tzinfo is None:
        raise ValueError('Timezone-aware observation required')
    level=Decimal(str(reference_level))
    if not level.is_finite() or level<=0:
        raise ValueError('Positive finite reference level required')
    if not candles:
        return FilterDecision(False,'ATR warmup incomplete')
    for bar in candles:
        if bar.time.tzinfo is None or bar.time+timedelta(minutes=5)>observed_at:
            raise ValueError('Only completed timezone-aware candles are allowed')
        values=(bar.open,bar.high,bar.low,bar.close)
        if not all(v.is_finite() and v>0 for v in values):
            raise ValueError('Positive finite Decimal OHLC required')
        if not bar.low<=min(bar.open,bar.close)<=max(bar.open,bar.close)<=bar.high:
            raise ValueError('Invalid OHLC')
        if bar.time.astimezone(IST).date()!=observed_at.astimezone(IST).date():
            raise ValueError('Only current-session candles are allowed')
    if (candles[0].time.astimezone(IST).time()!=time(9,15) or
            any(b.time-a.time!=timedelta(minutes=5) for a,b in zip(candles,candles[1:])) or
            observed_at-candles[-1].time>=timedelta(minutes=10)):
        return FilterDecision(False,'Incomplete or stale session candles')
    if len(candles)<14:
        return FilterDecision(False,'ATR warmup incomplete')
    ranges=[]
    for i,bar in enumerate(candles):
        ranges.append(bar.high-bar.low if i==0 else max(bar.high-bar.low,
                      abs(bar.high-candles[i-1].close),abs(bar.low-candles[i-1].close)))
    atr=sum(ranges[-14:])/14
    closes=[b.close for b in candles if b.time.astimezone(IST).time()>=time(9,45)]
    crossings=sum((a-level)*(b-level)<0 for a,b in zip(closes,closes[1:]))
    if atr>20:
        return FilterDecision(False,'Recent volatility exceeds 20 points',atr,crossings)
    if crossings>2:
        return FilterDecision(False,'Reference crossed more than twice',atr,crossings)
    return FilterDecision(True,'Volatility and level checks passed',atr,crossings)
