"""Closed Nifty candle feed, seeded from the same DB history as the backtests."""
from datetime import date,datetime,time,timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import text

from backtest.snapshot_cache import load_snapshot_candles,exchange_time,valid_ohlc
from backtest.nifty_30m_put_comparison import aggregate_session
from backtest.nifty_daily_ema import daily_from_minutes
from backtest.session_calendar import regular_session_closed
from strategies.nifty_bearish_15m import Bearish15m

IST=ZoneInfo('Asia/Kolkata')


class Calendar:
    def __init__(self,factory):
        with factory() as db:
            self.holidays={date.fromisoformat(str(r[0])) if isinstance(r[0],str) else r[0]
                           for r in db.execute(text('SELECT holiday_date FROM trading_holidays'))}

    def __call__(self,day):return not regular_session_closed(day) and day not in self.holidays

    def next_open(self,day):
        day+=timedelta(days=1)
        while not self(day):day+=timedelta(days=1)
        return datetime.combine(day,time(9,15),IST)


def schedule(signal_time,kind,calendar):
    if kind=='daily_calls' or signal_time.time()>=time(15,30):
        opening=calendar.next_open(signal_time.date())
    else:opening=signal_time
    return opening,opening+timedelta(minutes=15 if kind=='daily_calls' else 1,seconds=0 if kind=='daily_calls' else 30)


def completed_today(records,now):
    selected=[]
    opening=now.replace(hour=9,minute=15,second=0,microsecond=0)
    for row in records:
        stamp=exchange_time(row['date'])
        if stamp.date()!=now.date():raise ValueError('Wrong session in current-minute feed')
        if stamp+timedelta(minutes=1,seconds=2)>now:continue
        if not valid_ohlc(row):raise ValueError('Invalid current-minute candle')
        if stamp!=opening+timedelta(minutes=len(selected)):raise ValueError('Missing/duplicate/out-of-order current-session minute')
        selected.append(dict(row,date=stamp))
    # The last two minutes may still be finalising; a longer gap blocks decisions.
    expected=max(0,min(375,int((now-opening-timedelta(seconds=2)).total_seconds()//60)))
    if len(selected)<expected:raise ValueError('Current minute feed is stale or missing candles')
    return selected


class Feed:
    def __init__(self,kite,factory,kind,calendar,now,*,skip_incomplete_days=False):
        self.kite,self.kind,self.calendar=kite,kind,calendar
        self.day=now.date()
        self.excluded=[]
        minutes=load_snapshot_candles(date(2020,1,1),now.date()-timedelta(days=1),database_only=True,
            session_factory=factory,skip_incomplete_days=skip_incomplete_days,excluded_days=self.excluded)
        self.history=daily_from_minutes(minutes) if kind=='daily_calls' else aggregate_session(minutes,15)
        if not self.history:raise ValueError('No historical warmup available')
        expected=now.date()-timedelta(days=1)
        while not calendar(expected):expected-=timedelta(days=1)
        latest=self.history[-1]['date']
        latest=exchange_time(latest).date() if isinstance(latest,datetime) else date.fromisoformat(str(latest))
        if latest!=expected:
            raise ValueError(f'Latest completed session is missing: expected {expected}, got {latest}')
        self.token=next((i['instrument_token'] for i in kite.instruments('NSE') if i.get('tradingsymbol')=='NIFTY 50'),None)
        if self.token is None:raise ValueError('Nifty index token unavailable')

    def snapshots(self,now,after=None):
        if now.date()!=self.day:raise ValueError('Restart feed for the next session')
        results=[]
        if self.kind=='daily_calls':
            f=s=None
            for i,b in enumerate(self.history):
                pf,ps=f,s;c=float(b['close'])
                f=c if f is None else f+2/21*(c-f)
                s=c if s is None else s+2/51*(c-s)
                if i<50:continue
                stamp=datetime.combine(date.fromisoformat(str(b['date'])),time(15,30),IST)
                eligible,deadline=schedule(stamp,self.kind,self.calendar)
                results.append(dict(time=stamp,fast=f,slow=s,bullish=f>s,trend_valid=f>s,
                    enter=pf<=ps and f>s,exit=pf>=ps and f<s,eligible_after=eligible,deadline=deadline))
        else:
            if self.calendar(now.date()) and now.time()>=time(9,15):
                records=self.kite.historical_data(self.token,now.replace(hour=9,minute=15,second=0,microsecond=0),now,'minute')
                minutes=completed_today(records,now)
            else:
                minutes=[]
            current=[]
            for offset in range(0,len(minutes)//15*15,15):
                bucket=minutes[offset:offset+15]
                current.append(dict(date=bucket[0]['date'],open=bucket[0]['open'],close=bucket[-1]['close'],
                    high=max(b['high'] for b in bucket),low=min(b['low'] for b in bucket)))
            engine=Bearish15m()
            for b in self.history+current:
                state=engine.on_bar(b,observed_at=now)
                if not state['warmed_up']:continue
                eligible,deadline=schedule(state['signal_time'],self.kind,self.calendar)
                results.append(dict(time=state['signal_time'],fast=state['fast_ema'],slow=state['slow_ema'],
                    bullish=state['fast_ema']>state['slow_ema'],trend_valid=state['fast_ema']<state['slow_ema'],
                    enter=state['cross_down'],exit=state['cross_up'],eligible_after=eligible,deadline=deadline))
        if not results:raise ValueError('No warmed-up completed signal candles')
        if after:
            after=exchange_time(after)
            later=[r for r in results if r['time']>after]
            return later or [results[-1]]
        return [results[-1]]
