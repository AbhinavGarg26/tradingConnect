"""Shared deterministic EMA calculations; never stores positions or submits orders."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import date,datetime,time,timedelta,timezone
import hashlib
import json
from zoneinfo import ZoneInfo
from sqlalchemy import BigInteger,Date,DateTime,ForeignKey,Integer,String,UniqueConstraint,delete,select,text
from sqlalchemy.orm import Mapped,mapped_column
from trading.database import Base
from trading.strategy_order_intents import JSON_TYPE
from trading.ema_feed import load_snapshot_candles,daily_from_minutes,aggregate_session,schedule,completed_today,valid_ohlc
from backtest.session_calendar import REGULAR_SESSION_HOLIDAYS
from market.candle_polling import ClosedCandlePoller,is_rate_limit

IST=ZoneInfo('Asia/Kolkata')
PK=BigInteger().with_variant(Integer(),'sqlite')

class CacheStale(RuntimeError):pass

class IndicatorCacheRow(Base):
    __tablename__='strategy_indicator_caches'
    id: Mapped[int]=mapped_column(PK,primary_key=True)
    cache_key: Mapped[str]=mapped_column(String,unique=True,nullable=False)
    configuration: Mapped[dict]=mapped_column(JSON_TYPE,nullable=False)
    state: Mapped[dict]=mapped_column(JSON_TYPE,nullable=False,default=dict)
    source_through: Mapped[date|None]=mapped_column(Date)
    source_updated_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    last_candle_time: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    excluded_days: Mapped[list]=mapped_column(JSON_TYPE,nullable=False,default=list)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True))

class IndicatorCandleRow(Base):
    __tablename__='strategy_indicator_candles'
    __table_args__=(UniqueConstraint('cache_key','candle_time',name='idx_indicator_candles_identity'),)
    id: Mapped[int]=mapped_column(PK,primary_key=True)
    cache_key: Mapped[str]=mapped_column(String,ForeignKey('strategy_indicator_caches.cache_key'),nullable=False)
    candle_time: Mapped[datetime]=mapped_column(DateTime(timezone=True),nullable=False)
    trading_date: Mapped[date]=mapped_column(Date,nullable=False)
    state: Mapped[dict]=mapped_column(JSON_TYPE,nullable=False)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True))


def utc(stamp):
    return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)


def previous_session(now,calendar):
    day=now.date()-timedelta(days=1)
    while not calendar(day):day-=timedelta(days=1)
    return day


def configuration(kind,calendar,skip):
    if kind not in {'daily_calls','bearish_puts','bullish_calls'}:raise ValueError('Unknown EMA profile')
    fast,slow=(20,50) if kind=='daily_calls' else (9,21)
    holidays=sorted(str(d) for d in set(getattr(calendar,'holidays',set()))|set(REGULAR_SESSION_HOLIDAYS))
    result=dict(algorithm='completed-candle-float-ema-v1',kind=kind,fast=fast,slow=slow,
        timeframe='1d' if kind=='daily_calls' else '15m',seed='2020-01-01',
        source='market_snapshots/NIFTY 50/1m',skip_incomplete_days=skip,calendar_sha256=hashlib.sha256(json.dumps(holidays).encode()).hexdigest())
    if kind=='bearish_puts':
        from strategies.nifty_bearish_15m import BASELINE
        result['frozen_rules_sha256']=hashlib.sha256(BASELINE.read_bytes()).hexdigest()
    return result


class IndicatorCache:
    def __init__(self,factory,kind,calendar,*,skip_incomplete_days=False,read_only=False):
        self.factory,self.kind,self.calendar=factory,kind,calendar
        self.read_only=read_only
        self.configuration=configuration(kind,calendar,skip_incomplete_days)
        self.key=hashlib.sha256(json.dumps(self.configuration,sort_keys=True).encode()).hexdigest()
        self.data=None;self.ephemeral={}

    def _lock(self,db):
        if not self.read_only and db.bind.dialect.name=='postgresql':
            db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key,0))'),{'key':'ema-indicator:'+self.key})

    def _load(self,db,now,rebuild=False):
        row=db.scalar(select(IndicatorCacheRow).where(IndicatorCacheRow.cache_key==self.key))
        if rebuild:
            if self.read_only:raise ValueError('Read-only checks cannot rebuild caches')
            db.execute(delete(IndicatorCandleRow).where(IndicatorCandleRow.cache_key==self.key))
            if row:db.delete(row);db.flush()
            row=None
        if row:
            if row.configuration!=self.configuration:raise CacheStale('Cache configuration mismatch')
            return row,dict(state=deepcopy(row.state),source_through=row.source_through,
                source_updated_at=row.source_updated_at,last_candle_time=row.last_candle_time,
                excluded_days=list(row.excluded_days))
        if not self.read_only:
            row=IndicatorCacheRow(cache_key=self.key,configuration=self.configuration,state={},excluded_days=[],created_at=now,updated_at=now)
            db.add(row);db.flush()
        return row,dict(state={},source_through=None,source_updated_at=None,last_candle_time=None,excluded_days=[])

    def _watermark(self,db,through):
        end=datetime.combine(through+timedelta(days=1),time.min,IST).astimezone(timezone.utc).replace(tzinfo=None)
        start=datetime(2020,1,1,tzinfo=IST).astimezone(timezone.utc).replace(tzinfo=None)
        value=db.execute(text("""SELECT MAX(updated_at) FROM market_snapshots
            WHERE symbol='NIFTY 50' AND timeframe='1m' AND captured_at>=:start AND captured_at<:end"""),{'start':start,'end':end}).scalar()
        if isinstance(value,str):value=datetime.fromisoformat(value)
        return utc(value) if value else None

    def _save(self,db,row,data,now):
        if not self.read_only:
            for name,value in data.items():setattr(row,name,value)
            row.updated_at=now
            db.flush()
        self.data=data

    def _advance(self,db,data,bar,now,track=False):
        if not valid_ohlc(bar):raise ValueError('Invalid cached candle OHLC')
        stamp=bar['date']
        if isinstance(stamp,datetime):opening=stamp.astimezone(IST)
        else:opening=datetime.combine(date.fromisoformat(str(stamp)),time(9,15),IST)
        close_time=(datetime.combine(opening.date(),time(15,30),IST) if self.kind=='daily_calls' else opening+timedelta(minutes=15))
        if close_time>now:raise ValueError('Incomplete candle cannot enter the EMA cache')
        prices={key:float(bar[key]) for key in ('open','high','low','close')}
        if data['last_candle_time'] and utc(close_time)<=utc(data['last_candle_time']):
            state=self.ephemeral.get(utc(close_time).isoformat())
            if state is None:
                old=db.scalar(select(IndicatorCandleRow).where(IndicatorCandleRow.cache_key==self.key,IndicatorCandleRow.candle_time==close_time.astimezone(timezone.utc)))
                state=old.state if old else None
            if not state or state['bar']!=dict(date=opening.isoformat(),**prices):
                raise CacheStale('A previously cached candle changed; rebuild indicator cache after reviewing source corrections')
            return
        state=data['state']
        if self.kind in {'bearish_puts','bullish_calls'}:
            offset=(opening-opening.replace(hour=9,minute=15,second=0,microsecond=0)).total_seconds()/60
            if offset<0 or offset>360 or offset%15:raise ValueError('Cached 15m candle is not session aligned')
            if state:
                previous=datetime.fromisoformat(state['time'])
                if previous.date()==opening.date() and opening!=previous:
                    raise ValueError('Missing intraday candle before cached EMA update')
                if previous.date()!=opening.date() and offset!=0:
                    raise ValueError('A new session must begin with the opening candle')
            elif offset!=0:raise ValueError('EMA history must begin with the session opening candle')
        count=state.get('count',0)+1
        before_fast,before_slow=state.get('fast'),state.get('slow')
        f,s=self.configuration['fast'],self.configuration['slow'];close=prices['close']
        fast=close if before_fast is None else before_fast+2/(f+1)*(close-before_fast)
        slow=close if before_slow is None else before_slow+2/(s+1)*(close-before_slow)
        warmed=count>= (51 if self.kind=='daily_calls' else 21)
        state=dict(count=count,fast=fast,slow=slow,warmed_up=warmed,
            cross_up=bool(warmed and before_fast is not None and before_fast<=before_slow and fast>slow),
            cross_down=bool(warmed and before_fast is not None and before_fast>=before_slow and fast<slow),
            time=close_time.isoformat(),bar=dict(date=opening.isoformat(),**prices))
        data['state']=state;data['last_candle_time']=close_time.astimezone(timezone.utc)
        if self.read_only and track:self.ephemeral[utc(close_time).isoformat()]=deepcopy(state)
        if not self.read_only:
            db.add(IndicatorCandleRow(cache_key=self.key,candle_time=data['last_candle_time'],trading_date=opening.date(),state=deepcopy(state),created_at=now,updated_at=now))
            if count%500==0:db.flush()

    def sync_history(self,now,*,rebuild=False):
        end=previous_session(now,self.calendar)
        with self.factory() as db:
            self._lock(db);row,data=self._load(db,now,rebuild)
            if data['last_candle_time'] and utc(data['last_candle_time'])>utc(now):
                raise CacheStale('Cached candle is ahead of the current clock')
            if data['source_through']:
                actual=self._watermark(db,data['source_through'])
                saved=utc(data['source_updated_at']) if data['source_updated_at'] else None
                if actual!=saved:raise CacheStale('Historical source candles changed. Review corrections and rebuild the indicator cache; existing trading checkpoints are untouched')
            if not data['source_through'] or data['source_through']<end:
                watermark=self._watermark(db,end)
                start=data['source_through']+timedelta(days=1) if data['source_through'] else date(2020,1,1)
                @contextmanager
                def same_session():yield db
                latest_historical=None
                while start<=end:
                    last=min(start+timedelta(days=29),end);excluded=[]
                    minutes=load_snapshot_candles(start,last,database_only=True,session_factory=same_session,
                        skip_incomplete_days=self.configuration['skip_incomplete_days'],excluded_days=excluded)
                    bars=daily_from_minutes(minutes) if self.kind=='daily_calls' else aggregate_session(minutes,15)
                    for day in excluded:
                        if db.scalar(select(IndicatorCandleRow.id).where(IndicatorCandleRow.cache_key==self.key,IndicatorCandleRow.trading_date==day).limit(1)):
                            raise CacheStale('An incomplete historical session already has cached intraday bars; review and rebuild')
                    data['excluded_days']=sorted(set(data['excluded_days'])|{str(day) for day in excluded})
                    for bar in bars:self._advance(db,data,bar,now)
                    if bars:latest_historical=bars[-1]['date']
                    start=last+timedelta(days=1)
                latest_day=(latest_historical.date() if isinstance(latest_historical,datetime) else date.fromisoformat(str(latest_historical))) if latest_historical else None
                if latest_day!=end:raise ValueError(f'Latest completed session is missing: expected {end}, got {latest_day}')
                if self._watermark(db,end)!=watermark:raise CacheStale('Historical source changed during calculation; retry after source updates finish')
                data['source_through']=end;data['source_updated_at']=watermark
            if not data['state']:raise ValueError('No historical warmup available')
            self._save(db,row,data,now)
        return self.data

    def append(self,bars,now):
        with self.factory() as db:
            self._lock(db)
            if self.read_only:
                if self.data is None:raise ValueError('Initialize indicator history before appending')
                row=None;data=deepcopy(self.data)
            else:row,data=self._load(db,now)
            for bar in bars:self._advance(db,data,bar,now,track=True)
            self._save(db,row,data,now)

    def snapshots(self,now,after=None):
        states=[]
        if after is not None and not self.read_only:
            with self.factory() as db:
                states=[row.state for row in db.scalars(select(IndicatorCandleRow).where(IndicatorCandleRow.cache_key==self.key,IndicatorCandleRow.candle_time>utc(after)).order_by(IndicatorCandleRow.candle_time))]
        if not states:states=[self.data['state']]
        result=[]
        for state in states:
            if not state['warmed_up']:continue
            stamp=datetime.fromisoformat(state['time']);eligible,deadline=schedule(stamp,self.kind,self.calendar)
            bullish=state['fast']>state['slow']
            result.append(dict(kind=self.kind,time=stamp,fast=state['fast'],slow=state['slow'],bullish=bullish,
                trend_valid=bullish if self.kind in {'daily_calls','bullish_calls'} else state['fast']<state['slow'],
                enter=state['cross_up'] if self.kind in {'daily_calls','bullish_calls'} else state['cross_down'],
                exit=state['cross_down'] if self.kind in {'daily_calls','bullish_calls'} else state['cross_up'],eligible_after=eligible,deadline=deadline))
        if not result:raise ValueError('No warmed-up completed signal candles')
        return result


class CachedFeed:
    def __init__(self,kite,factory,kind,calendar,now,*,skip_incomplete_days=False,read_only=False):
        self.kite,self.kind,self.calendar=kite,kind,calendar;self.day=now.date();self.token=None
        self.cache=IndicatorCache(factory,kind,calendar,skip_incomplete_days=skip_incomplete_days,read_only=read_only)
        try:
            self.cache.sync_history(now)
        except ValueError as exc:
            if read_only or not str(exc).startswith('Latest completed session is missing:'):raise
            # Repair only the newly missing completed session, preserving frozen
            # historical exclusions and existing calculation checkpoints.
            from market.shared_nifty_candles import ensure_minutes
            end=previous_session(now,calendar)
            opening=datetime.combine(end,time(9,15),IST)
            ensure_minutes(kite,opening,opening.replace(hour=15,minute=30),now,factory=factory)
            self.cache.sync_history(now)
        self.excluded=[date.fromisoformat(day) for day in self.cache.data['excluded_days']]
        self.poller=ClosedCandlePoller(grace_seconds=5)

    def snapshots(self,now,after=None):
        if now.date()!=self.day:raise ValueError('Restart feed for the next session')
        asof=min(now,now.replace(hour=15,minute=30,second=5,microsecond=0))
        if self.kind in {'bearish_puts','bullish_calls'} and self.poller.retry_at and now<self.poller.retry_at:
            if self.cache.read_only:raise RuntimeError('Current database candles are incomplete; retry readiness later')
            return []
        if self.kind in {'bearish_puts','bullish_calls'} and self.calendar(now.date()) and self.poller.due(asof):
            last=self.cache.data['last_candle_time']
            opening=now.replace(hour=9,minute=15,second=0,microsecond=0)
            expected=opening+timedelta(minutes=self.poller.slot(asof)[1]*15)
            if last and utc(last)>=utc(expected):self.poller.success(asof)
            else:
                from market.database_candles import read_completed_15m,CandleNotReady
                try:
                    bars=read_completed_15m(asof,after=(utc(last).astimezone(IST)-timedelta(minutes=15) if last else None),
                        factory=self.cache.factory,persist=not self.cache.read_only,kite=self.kite)
                except CandleNotReady as exc:
                    self.poller.rate_limited(now)
                    if self.cache.read_only:raise RuntimeError('Current database candles are incomplete; retry readiness later') from exc
                    return []
                if not bars or bars[-1]['date']+timedelta(minutes=15)!=expected:raise ValueError('Latest completed 15m database candle not published')
                self.cache.append(bars,now);self.poller.success(asof)
        return self.cache.snapshots(now,after)
