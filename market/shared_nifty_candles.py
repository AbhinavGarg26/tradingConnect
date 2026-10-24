"""Single-flight, read-through storage of completed NIFTY minute candles.

Only market-data API reads occur here. All callers use the same PostgreSQL
transaction lock, unique snapshot identity, and persistent failure cooldown.
"""
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from zoneinfo import ZoneInfo
from sqlalchemy import BigInteger,Integer,String,DateTime,Numeric,bindparam,select,text
from sqlalchemy.orm import Mapped,mapped_column
from trading.database import Base
from backtest.snapshot_cache import valid_ohlc,exchange_time

IST=ZoneInfo('Asia/Kolkata');SOURCE='NIFTY 50/1m';TOKEN=256265
class FetchState(Base):
    __tablename__='market_candle_fetch_states'
    id: Mapped[int]=mapped_column(BigInteger().with_variant(Integer(),'sqlite'),primary_key=True)
    source_key: Mapped[str]=mapped_column(String,unique=True)
    last_attempt_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str|None]=mapped_column(String)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True))

def _utc(stamp):return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
def _naive(stamp):return _utc(stamp).replace(tzinfo=None)

def _missing(db,expected):
    rows=db.execute(text("""SELECT captured_at,open_price,high_price,low_price,close_price FROM market_snapshots
        WHERE symbol='NIFTY 50' AND timeframe='1m' AND captured_at>=:start AND captured_at<:end"""),
        {'start':_naive(min(expected)),'end':_naive(max(expected)+timedelta(minutes=1))})
    valid=set()
    for row in rows:
        stamp=row[0]
        if isinstance(stamp,str):stamp=datetime.fromisoformat(stamp)
        if valid_ohlc(dict(zip(('open','high','low','close'),row[1:]))):valid.add(_utc(stamp))
    return sorted(expected-valid)

def ensure_minutes(kite,start,end,now,*,factory=None):
    """Ensure [start,end) exists, bounded to one regular session, completed only.

    Recheck after locking, so a waiting strategy sees the first caller's commit.
    Failures commit a cooldown before returning; readers then fail closed.
    """
    if factory is None:
        from trading.database import get_db
        factory=get_db
    start,end,now=(s.astimezone(IST) for s in (start,end,now))
    opening=start.replace(hour=9,minute=15,second=0,microsecond=0)
    closing=opening.replace(hour=15,minute=30)
    if start.second or start.microsecond or end.second or end.microsecond or start<opening or end>closing or start>=end or start.date()!=end.date():
        raise ValueError('Candle fill range must be aligned within one NIFTY session')
    if end+timedelta(seconds=5)>now:raise ValueError('Cannot fetch an incomplete minute')
    expected={_utc(start+timedelta(minutes=i)) for i in range(int((end-start).total_seconds()//60))}
    with factory() as db:
        if db.bind.dialect.name=='postgresql':
            db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key,0))'),{'key':'market-candle-source:'+SOURCE})
        missing=_missing(db,expected)
        if not missing:return dict(complete=True,fetched=0,saved=0)
        state=db.scalar(select(FetchState).where(FetchState.source_key==SOURCE))
        if state and state.next_attempt_at and _utc(state.next_attempt_at)>_utc(now):
            return dict(complete=False,fetched=0,saved=0,cooldown=True)
        if state is None:
            state=FetchState(source_key=SOURCE,created_at=_utc(now),updated_at=_utc(now));db.add(state)
        state.last_attempt_at=state.updated_at=_utc(now)
        state.next_attempt_at=_utc(now)+timedelta(seconds=10)
        state.last_error_code=None
        try:
            records=kite.historical_data(TOKEN,min(missing).astimezone(IST),(max(missing)+timedelta(minutes=1)).astimezone(IST),'minute')
        except Exception as exc:
            state.last_error_code=type(exc).__name__
            state.next_attempt_at=_utc(now)+timedelta(seconds=30)
            return dict(complete=False,fetched=1,saved=0,error=type(exc).__name__)
        prepared={};wanted=set(missing)
        for record in records:
            stamp=exchange_time(record['date']).astimezone(timezone.utc)
            if stamp not in wanted:continue
            if stamp in prepared or not valid_ohlc(record):
                state.last_error_code='InvalidCandleResponse'
                return dict(complete=False,fetched=1,saved=0,error='InvalidCandleResponse')
            prepared[stamp]={k:Decimal(str(record[k])) for k in ('open','high','low','close')}
        # Upsert one canonical row. Do not touch timestamps on unchanged candles.
        saved=0
        for stamp,prices in prepared.items():
            result=db.execute(text("""INSERT INTO market_snapshots
                (symbol,timeframe,captured_at,ltp,open_price,high_price,low_price,close_price,created_at,updated_at)
                VALUES ('NIFTY 50','1m',:stamp,:close,:open,:high,:low,:close,:now,:now)
                ON CONFLICT (symbol,timeframe,captured_at) DO UPDATE SET
                ltp=excluded.ltp,open_price=excluded.open_price,high_price=excluded.high_price,
                low_price=excluded.low_price,close_price=excluded.close_price,updated_at=excluded.updated_at
                WHERE market_snapshots.open_price IS DISTINCT FROM excluded.open_price
                   OR market_snapshots.high_price IS DISTINCT FROM excluded.high_price
                   OR market_snapshots.low_price IS DISTINCT FROM excluded.low_price
                   OR market_snapshots.close_price IS DISTINCT FROM excluded.close_price""").bindparams(*[bindparam(k,type_=Numeric(16,4)) for k in ('open','high','low','close')]),dict(prices,stamp=_naive(stamp),now=_naive(now)))
            saved+=max(0,result.rowcount)
        missing_after=_missing(db,expected)
        if not missing_after:state.next_attempt_at=None
        else:state.last_error_code='IncompleteCandleResponse'
        return dict(complete=not missing_after,fetched=1,saved=saved)


def sync_current_session(kite,now,*,factory=None):
    from backtest.session_calendar import regular_session_closed
    if regular_session_closed(now.astimezone(IST).date()):return dict(complete=True,fetched=0,saved=0)
    now=now.astimezone(IST);opening=now.replace(hour=9,minute=15,second=0,microsecond=0)
    end=min(now.replace(second=0,microsecond=0),opening.replace(hour=15,minute=30))
    if end+timedelta(seconds=5)>now:end-=timedelta(minutes=1)
    if end<=opening:return dict(complete=True,fetched=0,saved=0)
    return ensure_minutes(kite,opening,end,now,factory=factory)
