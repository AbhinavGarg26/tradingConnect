"""Database-first completed NIFTY candles with shared missing-minute backfill."""
from datetime import datetime,timedelta,timezone
from decimal import Decimal
import hashlib,json
from zoneinfo import ZoneInfo
from sqlalchemy import select,text

IST=ZoneInfo('Asia/Kolkata')
CONFIG={'algorithm':'closed-nifty-15m-from-db-minutes-v1','source':'market_snapshots/NIFTY 50/1m','timeframe':'15m','grace_seconds':5}
KEY=hashlib.sha256(json.dumps(CONFIG,sort_keys=True).encode()).hexdigest()
class CandleNotReady(RuntimeError):pass


def _local(stamp):
    if isinstance(stamp,str):stamp=datetime.fromisoformat(stamp)
    if stamp.tzinfo is None:stamp=stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(IST)


def _read_completed_15m(now,*,after=None,factory=None,persist=True):
    """Return only completed bars newer than `after` (an opening timestamp).

    Build uncached buckets only, requiring every minute and valid OHLC. One
    transaction publishes all requested bars, preventing partial/stale decisions.
    Cache rows are price bars, not EMA settings or execution checkpoints.
    """
    from trading.ema_indicator_cache import IndicatorCacheRow,IndicatorCandleRow
    from backtest.snapshot_cache import valid_ohlc
    if factory is None:
        from trading.database import get_db
        factory=get_db
    now=now.astimezone(IST);opening=now.replace(hour=9,minute=15,second=0,microsecond=0)
    slots=min(25,max(0,int((now-opening-timedelta(seconds=5)).total_seconds()//900)))
    stamps=[opening+timedelta(minutes=15*i) for i in range(slots)]
    if after is not None:stamps=[stamp for stamp in stamps if stamp>_local(after)]
    if not stamps:return []
    with factory() as db:
        if persist and db.bind.dialect.name=='postgresql':
            db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key,0))'),{'key':'db-bars:'+KEY})
        head=db.scalar(select(IndicatorCacheRow).where(IndicatorCacheRow.cache_key==KEY))
        if head and head.configuration!=CONFIG:raise RuntimeError('Database candle cache configuration mismatch')
        cached={_local(row.candle_time)-timedelta(minutes=15):row.state for row in db.scalars(
            select(IndicatorCandleRow).where(IndicatorCandleRow.cache_key==KEY,
                IndicatorCandleRow.candle_time>=(stamps[0]+timedelta(minutes=15)).astimezone(timezone.utc),
                IndicatorCandleRow.candle_time<=(stamps[-1]+timedelta(minutes=15)).astimezone(timezone.utc)))}
        missing=[s for s in stamps if s not in cached];built={}
        if missing:
            params={'start':missing[0].astimezone(timezone.utc).replace(tzinfo=None),
                    'end':(missing[-1]+timedelta(minutes=15)).astimezone(timezone.utc).replace(tzinfo=None)}
            rows=db.execute(text("""SELECT captured_at,open_price,high_price,low_price,close_price
                FROM market_snapshots WHERE symbol='NIFTY 50' AND timeframe='1m'
                AND captured_at>=:start AND captured_at<:end ORDER BY captured_at"""),params)
            minutes={}
            for row in rows:
                stamp=_local(row[0])
                if stamp.second or stamp.microsecond:raise CandleNotReady('NIFTY database minute is not aligned')
                if stamp in minutes:raise CandleNotReady('Duplicate NIFTY minute in database')
                prices=dict(zip(('open','high','low','close'),row[1:]))
                if not valid_ohlc(prices):raise CandleNotReady('Invalid NIFTY minute OHLC in database')
                minutes[stamp]=prices
            for stamp in missing:
                expected=[stamp+timedelta(minutes=i) for i in range(15)]
                if any(s not in minutes for s in expected):
                    raise CandleNotReady(f'Database missing complete NIFTY minute bucket ending {stamp+timedelta(minutes=15)}')
                group=[minutes[s] for s in expected]
                built[stamp]={'time':(stamp+timedelta(minutes=15)).isoformat(),'bar':dict(date=stamp.isoformat(),
                    open=float(group[0]['open']),high=float(max(r['high'] for r in group)),
                    low=float(min(r['low'] for r in group)),close=float(group[-1]['close']))}
            if persist:
                if head is None:
                    head=IndicatorCacheRow(cache_key=KEY,configuration=CONFIG,state={},excluded_days=[],created_at=now,updated_at=now)
                    db.add(head);db.flush()
                for stamp,state in built.items():
                    db.add(IndicatorCandleRow(cache_key=KEY,candle_time=(stamp+timedelta(minutes=15)).astimezone(timezone.utc),
                        trading_date=stamp.date(),state=state,created_at=now,updated_at=now))
                latest=max(built)
                if head.last_candle_time is None or _local(head.last_candle_time)<latest+timedelta(minutes=15):
                    head.last_candle_time=(latest+timedelta(minutes=15)).astimezone(timezone.utc);head.source_through=latest.date()
                head.state={'count':head.state.get('count',0)+len(built),'time':_local(head.last_candle_time).isoformat()}
                head.updated_at=now
        result=[]
        for stamp in stamps:
            state=built.get(stamp) or cached[stamp];bar=state['bar']
            if not valid_ohlc(bar) or _local(bar['date'])!=stamp or _local(state['time'])!=stamp+timedelta(minutes=15):
                raise RuntimeError('Invalid stored database candle; review/rebuild candle cache')
            result.append(dict(date=stamp,**{k:Decimal(str(bar[k])) for k in ('open','high','low','close')}))
        return result


def read_completed_15m(now,*,after=None,factory=None,persist=True,kite=None):
    try:
        return _read_completed_15m(now,after=after,factory=factory,persist=persist)
    except CandleNotReady:
        if not persist or kite is None:raise
        from market.shared_nifty_candles import ensure_minutes
        opening=now.astimezone(IST).replace(hour=9,minute=15,second=0,microsecond=0)
        slots=min(25,max(0,int((now-opening-timedelta(seconds=5)).total_seconds()//900)))
        start=max(opening,_local(after)+timedelta(minutes=15)) if after else opening
        end=opening+timedelta(minutes=slots*15)
        if start>=end:raise
        ensure_minutes(kite,start,end,now,factory=factory)
        return _read_completed_15m(now,after=after,factory=factory,persist=persist)


def read_completed_minutes(now,*,after=None,kite=None,factory=None,persist=True):
    """Canonical 1m candles for every strategy, with the same fetch lock."""
    from backtest.snapshot_cache import valid_ohlc
    if factory is None:
        from trading.database import get_db
        factory=get_db
    now=now.astimezone(IST);opening=now.replace(hour=9,minute=15,second=0,microsecond=0)
    end=min((now-timedelta(seconds=5)).replace(second=0,microsecond=0),opening.replace(hour=15,minute=30))
    start=max(opening,_local(after)+timedelta(minutes=1)) if after is not None else opening
    if start>=end:return []
    expected=[start+timedelta(minutes=i) for i in range(int((end-start).total_seconds()//60))]
    def read():
        with factory() as db:
            rows=list(db.execute(text("""SELECT captured_at,open_price,high_price,low_price,close_price
                FROM market_snapshots WHERE symbol='NIFTY 50' AND timeframe='1m'
                AND captured_at>=:start AND captured_at<:end ORDER BY captured_at"""),
                {'start':start.astimezone(timezone.utc).replace(tzinfo=None),'end':end.astimezone(timezone.utc).replace(tzinfo=None)}))
        if len(rows)!=len(expected):raise CandleNotReady('Shared database minute feed is incomplete')
        result=[]
        for stamp,row in zip(expected,rows):
            prices=dict(zip(('open','high','low','close'),row[1:]))
            if _local(row[0])!=stamp or not valid_ohlc(prices):raise CandleNotReady('Invalid or missing shared database minute')
            result.append(dict(date=stamp,**{k:Decimal(str(v)) for k,v in prices.items()}))
        return result
    try:return read()
    except CandleNotReady:
        if not persist or kite is None:raise
        from market.shared_nifty_candles import ensure_minutes
        ensure_minutes(kite,start,end,now,factory=factory)
        return read()


class MinuteFeed:
    """Poll new database boundaries only; keep quote-driven exits responsive."""
    def __init__(self,kite,*,factory=None,persist=True):
        self.kite,self.factory,self.persist=kite,factory,persist
        self.last_boundary=None;self.retry_at=None;self.failed_since=None;self.blocked=False
    def read(self,now,after=None):
        boundary=(now-timedelta(seconds=5)).replace(second=0,microsecond=0)
        if self.retry_at and now<self.retry_at:return []
        if self.last_boundary==boundary:return []
        try:
            result=read_completed_minutes(now,after=after,kite=self.kite,factory=self.factory,persist=self.persist)
        except CandleNotReady:
            self.blocked=True
            if self.failed_since is None:self.failed_since=now
            if now-self.failed_since>=timedelta(seconds=90):
                raise RuntimeError('Shared minute candles unavailable for 90 seconds; stop and reconcile')
            self.retry_at=now+timedelta(seconds=10)
            return []
        self.blocked=False;self.retry_at=self.failed_since=None;self.last_boundary=boundary
        return result
