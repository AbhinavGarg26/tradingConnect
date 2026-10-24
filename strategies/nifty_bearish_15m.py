"""Frozen 15m EMA9/21 bearish signal engine. No broker/order capability."""
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import hashlib
import json

from backtest.snapshot_cache import exchange_time,valid_ohlc

KEY='nifty_bearish_15m_9_21'
VERSION='bearish_15m_9_21_v1'
BASELINE=Path(__file__).resolve().parent/'nifty_bearish_15m_9_21_frozen.json'


@dataclass(frozen=True)
class ExecutionProfile:
    """Reviewable candidate; does not enable orders or change signal rules."""
    lot_count: int = 1
    max_premium: Decimal = Decimal('30000')
    contract_side: str = 'PE'
    expiry: str = 'monthly_from_listed_futures'
    roll_sessions: int = 2
    activation: str = 'disabled'

    def __post_init__(self):
        if (self.lot_count!=1 or self.contract_side!='PE' or self.activation!='disabled'
            or not self.max_premium.is_finite() or not 0<self.max_premium<=Decimal('30000')
            or self.roll_sessions!=2 or self.expiry!='monthly_from_listed_futures'):
            raise ValueError('Unsupported preparation profile; real orders are not enabled')


def frozen_rules():
    value=json.loads(BASELINE.read_text())
    expected=dict(fast=9,slow=21,timeframe_minutes=15)
    if any(value['rules'].get(k)!=v for k,v in expected.items()):
        raise ValueError('Frozen strategy identity changed')
    return value


class Bearish15m:
    def __init__(self):
        self.baseline=frozen_rules()
        self.fast=self.slow=None
        self.count=0
        self.previous_open=None
        self.last=None

    def on_bar(self,row,*,observed_at):
        stamp=exchange_time(row['date'])
        now=exchange_time(observed_at)
        opening=stamp.replace(hour=9,minute=15,second=0,microsecond=0)
        offset=(stamp-opening).total_seconds()/60
        if not 0<=offset<=360 or offset%15:
            raise ValueError('Expected a session-aligned 15-minute candle opening time')
        if stamp+timedelta(minutes=15)>now:
            raise ValueError('Incomplete candle cannot generate a signal')
        if self.previous_open is not None and stamp<=self.previous_open:
            raise ValueError('Candles must be unique and chronological')
        if self.previous_open is not None and stamp.date()==self.previous_open.date() and stamp-self.previous_open!=timedelta(minutes=15):
            raise ValueError('Missing intraday candle; do not invent EMA history')
        if not valid_ohlc(row):
            raise ValueError('Invalid candle OHLC')
        before_fast,before_slow=self.fast,self.slow
        close=float(row['close'])
        self.fast=close if self.fast is None else self.fast+2/10*(close-self.fast)
        self.slow=close if self.slow is None else self.slow+2/22*(close-self.slow)
        self.count+=1
        self.previous_open=stamp
        cross_down=self.count>=21 and before_fast>=before_slow and self.fast<self.slow
        cross_up=self.count>=21 and before_fast<=before_slow and self.fast>self.slow
        self.last=dict(candle_open=stamp,signal_time=stamp+timedelta(minutes=15),
                       close=close,fast_ema=self.fast,slow_ema=self.slow,
                       previous_fast=before_fast,previous_slow=before_slow,
                       cross_down=cross_down,cross_up=cross_up,
                       trend='bearish' if self.fast<self.slow else 'bullish' if self.fast>self.slow else 'neutral',
                       warmed_up=self.count>=21)
        return self.last


def decision(state,*,owned_put=False):
    if not state['warmed_up']:
        return 'warmup'
    if owned_put:
        return 'exit_put' if state['cross_up'] else 'hold_put'
    return 'enter_put' if state['cross_down'] else 'wait'


def readiness(rows,now,*,owned_put=False):
    engine=Bearish15m()
    now=exchange_time(now)
    for row in rows:
        engine.on_bar(row,observed_at=now)
    if engine.last is None:
        raise ValueError('No completed candles supplied')
    state=engine.last
    within_session=now.weekday()<5 and now.hour>=9 and now<now.replace(hour=15,minute=30,second=0,microsecond=0)
    age=(now-state['signal_time']).total_seconds()
    actionable=within_session and 0<=age<=90 and state['warmed_up']
    return dict(strategy_key=KEY,strategy_version=VERSION,mode='read_only',
                activation='disabled',real_orders_enabled=False,state=state,
                historical_decision=decision(state,owned_put=owned_put),
                current_decision=decision(state,owned_put=owned_put) if actionable else 'wait_for_fresh_completed_candle',
                signal_fresh=actionable,baseline_sha256=hashlib.sha256(BASELINE.read_bytes()).hexdigest())
