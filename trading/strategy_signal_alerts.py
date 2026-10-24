"""Account-scoped alerts from committed strategy events, outside order execution.

Claim before HTTP send; ambiguous delivery is retained, never automatically
resent. This prevents restart duplicates at the cost of possible missed delivery
if the process dies after claiming. Telegram has no idempotent send endpoint.
"""
from datetime import datetime,timedelta,timezone
import hashlib
from html import escape
import logging
from zoneinfo import ZoneInfo
import httpx
from sqlalchemy import BigInteger,Integer,String,DateTime,select,text
from sqlalchemy.orm import Mapped,mapped_column
from trading.database import Base
from trading.ema_execution import aware
from trading.strategy_records import StrategyEvent,StrategyRun

log=logging.getLogger(__name__)
PK=BigInteger().with_variant(Integer(),'sqlite')
NAMES={'nifty_daily_ema_monthly':'NIFTY Daily EMA 20/50 Calls',
'nifty_bullish_15m_9_21':'NIFTY 15m EMA 9/21 Calls','nifty_bearish_15m_9_21':'NIFTY 15m EMA 9/21 Puts','nifty_15m_breakout':'Nifty 15m Breakout v3',
'nifty_30m_reference':'NIFTY 30m Body Reference PE1-V1','fixed_profit_filtered':'Fixed Profit Filtered'}

class Subscription(Base):
    __tablename__='strategy_telegram_subscriptions'
    id: Mapped[int]=mapped_column(PK,primary_key=True)
    user_id: Mapped[int]=mapped_column(BigInteger,unique=True)
    enabled_at: Mapped[datetime]=mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True))

class Delivery(Base):
    __tablename__='strategy_telegram_deliveries'
    id: Mapped[int]=mapped_column(PK,primary_key=True)
    user_id: Mapped[int]=mapped_column(BigInteger)
    strategy_event_id: Mapped[int]=mapped_column(BigInteger)
    dedupe_key: Mapped[str]=mapped_column(String,unique=True)
    status: Mapped[str]=mapped_column(String)
    sent_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    error_code: Mapped[str|None]=mapped_column(String)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True))


def enable(factory,user_id,now):
    """Explicit opt-in; no replay of signals that predate authorization."""
    with factory() as db:
        if not db.scalar(select(Subscription).where(Subscription.user_id==user_id)):
            db.add(Subscription(user_id=user_id,enabled_at=now,created_at=now,updated_at=now))


def notification(event,run):
    if run.strategy_key not in NAMES or run.mode not in {'paper','live'}:return None
    d=event.details or {};action=event.action
    mode=d.get('mode',run.mode)
    if mode not in {'paper','live'}:return None
    if action in {'DAILY_EMA_SIGNAL','BEARISH_EMA_SIGNAL','BULLISH_EMA_SIGNAL'}:
        decision=d.get('decision')
        if decision not in {'entry','exit','roll_entry','roll_exit'}:return None
        phase='SIGNAL';side=d.get('side') or ('CE' if run.strategy_key in {'nifty_daily_ema_monthly','nifty_bullish_15m_9_21'} else 'PE')
    elif action in {'BUY_CE','BUY_PE','EXIT_CE','EXIT_PE','ENTRY_INTENT','EXIT_INTENT'}:
        phase='CONFIRMED FILL' if d.get('intent_key') else 'SIGNAL'
        decision=event.reason if d.get('intent_key') else ('entry' if action.startswith(('BUY','ENTRY')) else 'exit')
        side=d.get('side') or (action[-2:] if action[-2:] in {'CE','PE'} else '')
    elif action in {'ENTRY_FILLED','EXIT_FILLED'}:
        phase='CONFIRMED FILL';decision='entry' if action=='ENTRY_FILLED' else 'exit';side=d.get('side','')
    else:return None
    # Duplicate source rows for the same signal share one durable delivery key.
    stamp=aware(event.occurred_at).isoformat()
    identity=f'{run.user_id}|{run.strategy_key}|{mode}|{stamp}|{phase}|{decision}|{side}'
    key=hashlib.sha256(identity.encode()).hexdigest()
    lines=[f'<b>{mode.upper()} · {phase}</b>',escape(NAMES[run.strategy_key]),
           escape(f'{decision.upper()} {side}'.strip()),
           aware(event.occurred_at).astimezone(ZoneInfo('Asia/Kolkata')).strftime('%d %b %Y %H:%M:%S IST')]
    if phase=='SIGNAL':lines.append('Signal recorded; execution/fill is not confirmed.')
    for label,value in [('Option',d.get('option_symbol') or d.get('symbol')),('Quantity',d.get('quantity')),
                        ('Premium',d.get('premium')),('Index',event.index_price),('Reason',event.reason)]:
        if value is not None:lines.append(f'{label}: {escape(str(value))}')
    return key,'\n'.join(lines)


def dispatch(factory,user_id,now,alerter=None):
    """Runs from the existing minute observer. No broker imports or order calls."""
    with factory() as db:
        subscription=db.scalar(select(Subscription).where(Subscription.user_id==user_id))
        if not subscription:return dict(enabled=False,sent=0)
        enabled_at=aware(subscription.enabled_at)
        if alerter is None:
            from trading.alerts import Alerter
            alerter=Alerter.from_db(db,user_id)
    if not alerter._enabled:return dict(enabled=True,configured=False,sent=0)
    cutoff=max(enabled_at,now-timedelta(minutes=15));items=[]
    with factory() as db:
        rows=db.execute(select(StrategyEvent,StrategyRun).join(StrategyRun,StrategyRun.id==StrategyEvent.strategy_run_id)
            .where(StrategyRun.user_id==user_id,StrategyEvent.created_at>=cutoff,StrategyEvent.occurred_at>=cutoff,
                   StrategyEvent.occurred_at<=now).order_by(StrategyEvent.id).limit(1000))
        for event,run in rows:
            item=notification(event,run)
            if item:items.append((event.id,*item))
    sent=0
    for event_id,key,message in items:
        with factory() as db:
            if db.bind.dialect.name=='postgresql':
                db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key,0))'),{'key':'telegram:'+key})
            if db.scalar(select(Delivery.id).where(Delivery.dedupe_key==key)):continue
            row=Delivery(user_id=user_id,strategy_event_id=event_id,dedupe_key=key,status='sending',created_at=now,updated_at=now)
            db.add(row);db.flush();delivery_id=row.id
        status,error='uncertain',None
        try:
            response=httpx.post(alerter.TELEGRAM_URL.format(token=alerter.bot_token),
                json={'chat_id':alerter.chat_id,'text':message,'parse_mode':'HTML'},timeout=5)
            if response.status_code==200 and response.json().get('ok') is True:status='sent';sent+=1
            else:status='failed';error='telegram_rejected'
        except Exception as exc:
            # Exception URLs may contain the secret bot token; never log them.
            error=type(exc).__name__
        with factory() as db:
            row=db.get(Delivery,delivery_id);row.status=status;row.error_code=error;row.updated_at=now
            if status=='sent':row.sent_at=now
        if status!='sent':log.warning('Strategy Telegram delivery %s: %s (%s)',delivery_id,status,error)
    return dict(enabled=True,configured=True,sent=sent)
