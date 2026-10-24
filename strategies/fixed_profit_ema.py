"""Experimental v3: opening-range re-entry rejection with a closed 30m EMA."""
from datetime import time, timedelta
from decimal import Decimal

from strategies.fixed_profit import FixedProfit
from strategies.nifty_opening_range import IST

STRATEGY_NAME = 'fixed_profit_ema_reentry'
STRATEGY_VERSION = 'v3-research'


class FixedProfitEMA(FixedProfit):
    """5m wick crosses opening boundary and closes directionally inside.

    CE rejects the opening low and must be above a completed-30m EMA.
    PE rejects the opening high and must be below it. Optional EMA slope filter.
    Risk, session limits, and confirmed-fill lifecycle are inherited from v2.
    """
    def __init__(self, *, require_ema=True, require_slope=False):
        super().__init__(min_range_points=40, confirmation_minutes=5,
                         tolerance_points=2, setup_mode='rejection', follow_through=False, stop_points=10)
        self.require_ema = require_ema
        self.require_slope = require_slope
        self.trend = None

    def set_trend(self, value, previous, *, closed_at):
        values = tuple(Decimal(str(v)) for v in (value, previous))
        if not all(v.is_finite() and v > 0 for v in values) or closed_at.tzinfo is None:
            raise ValueError('Positive finite EMA and timezone-aware close required')
        if self.trend and closed_at < self.trend[2]:
            raise ValueError('EMA updates must be chronological')
        self.trend = (*values, closed_at)

    def _trend_valid(self, price, side, observed_at=None):
        if not self.require_ema:
            return True
        if not self.trend:
            return False
        value, previous, closed_at = self.trend
        if observed_at is not None and closed_at > observed_at:
            raise ValueError('Future EMA cannot confirm this candle')
        return side*(price-value)>0 and (not self.require_slope or side*(value-previous)>0)

    def on_bar(self, bar, *, observed_at):
        self._validate_bar(bar)
        if not self.reference:
            raise ValueError('Set completed opening reference first')
        end = bar.time + timedelta(minutes=5)
        if observed_at.tzinfo is None or observed_at < end:
            raise ValueError('Confirmation candle must be closed')
        start = bar.time.astimezone(IST)
        if (start.date() != self.reference.time.astimezone(IST).date() or
                observed_at.astimezone(IST).date() != start.date()):
            raise ValueError('Confirmation must belong to current session')
        if bar.time < self.last_bar_end:
            raise ValueError('Candles must be chronological and non-overlapping')
        self.last_bar_end = end
        if (not self.eligible or self.position or self.pending or self.entries>=1 or
                end.astimezone(IST).time()>=time(14,30)):
            return None
        high, low = self.reference.high, self.reference.low
        if not low < bar.close < high:
            return None
        side = (1 if bar.low<=low and bar.close>bar.open else
                -1 if bar.high>=high and bar.close<bar.open else 0)
        if not side or not self._trend_valid(bar.close,side,end):
            return None
        return self._signal(end,bar.close,(side,low if side==1 else high,
                                          bar.low if side==1 else bar.high))

    def confirm_entry(self, *, index_price, option_fill=1):
        price = Decimal(str(index_price))
        if not self.entry_setup or not self._trend_valid(price,self.entry_setup[0]):
            raise ValueError('Fill no longer follows EMA')
        if not self.reference.low < price < self.reference.high:
            raise ValueError('Re-entry fill must remain inside opening range')
        return super().confirm_entry(index_price=price,option_fill=option_fill)
