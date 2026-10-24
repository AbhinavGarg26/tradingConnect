"""Frozen candidate orchestration: original entries, ATR/chop gate, 15/30 risk."""
from datetime import time, timedelta
from decimal import Decimal
import hashlib
import json
from types import MappingProxyType

from strategies.fixed_profit import FixedProfit
from strategies.fixed_profit_selective import evaluate_entry
from strategies.nifty_15m_breakout import Bar
from strategies.nifty_opening_range import IST

KEY='fixed_profit_filtered'
VERSION='v1-fixed-entries'
CONFIG=MappingProxyType({'reference_minutes':30,'confirmation_minutes':5,'min_range_points':40,
        'retest_tolerance_points':2,'entry_stop_points':10,'stop_points':15,
        'target_points':30,'max_entries':1,'atr_period':14,'atr_max':20,
        'max_crossings':2,'skip_day_on_filter_rejection':True,
        'entry_cutoff':'14:30','square_off':'15:15','lot_count':1,
        'contract':'nearest_unexpired_NIFTY_ATM','ema_filter':False,'risk_anchor':'pre_order_index_quote'})
CONFIG_HASH=hashlib.sha256(json.dumps(dict(CONFIG),sort_keys=True).encode()).hexdigest()


def aggregate(bars):
    return Bar(bars[0].time,bars[0].open,max(b.high for b in bars),
               min(b.low for b in bars),bars[-1].close)


class FilteredSession:
    """Feed consecutive completed minute bars from 09:15; quote exits separately.

    Filtering happens only after a fresh quote qualifies the original entry.
    An invalid fill quote rejects the signal without consuming the day, matching
    the baseline. A valid original entry rejected by the filter consumes the day.
    """
    def __init__(self):
        self.engine=FixedProfit(min_range_points=40,confirmation_minutes=5,tolerance_points=2,
                                setup_mode='retest',follow_through=False,stop_points=15,entry_stop_points=10)
        self.minutes=[];self.five=[];self.skipped=False;self.filter_decision=None

    def on_minute(self,bar,*,observed_at):
        self.engine._validate_bar(bar)
        if observed_at.tzinfo is None or bar.time+timedelta(minutes=1)>observed_at:
            raise ValueError('Only completed minute candles allowed')
        if not self.minutes:
            if bar.time.astimezone(IST).time()!=time(9,15):
                raise ValueError('Session must start at 09:15')
        elif bar.time-self.minutes[-1].time!=timedelta(minutes=1):
            raise ValueError('Missing, duplicate, or nonchronological minute candle')
        if self.minutes and bar.time.astimezone(IST).date()!=self.minutes[0].time.astimezone(IST).date():
            raise ValueError('New session requires a new controller')
        self.minutes.append(bar)
        n=len(self.minutes)
        if n%5==0:self.five.append(aggregate(self.minutes[-5:]))
        if n==30:
            self.engine.set_reference(aggregate(self.minutes),observed_at=bar.time+timedelta(minutes=1))
        elif n>30 and n%5==0 and not self.skipped:
            return self.engine.on_bar(self.five[-1],observed_at=bar.time+timedelta(minutes=1))
        return None

    def qualify_entry(self,quote,*,observed_at,max_signal_age=15):
        engine=self.engine;signal=engine.pending
        if not signal or not signal.action.startswith('BUY_'):
            return False
        age=(observed_at-signal.time).total_seconds()
        if age<0 or age>max_signal_age:
            engine.reject_signal();self.skipped=True;return False
        price=Decimal(str(quote))
        if not price.is_finite() or price<=0:
            raise ValueError('Invalid entry quote')
        if not engine._entry_valid(price,engine.entry_setup):
            engine.reject_signal();return False
        self.filter_decision=evaluate_entry(self.five,engine.entry_setup[1],observed_at=observed_at)
        if not self.filter_decision.allowed:
            engine.reject_signal();self.skipped=True;return False
        return True
