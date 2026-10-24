"""Durable intent reservation. Contains no broker calls or activation capability."""
from datetime import datetime,timezone
from decimal import Decimal
import hashlib
import json

from sqlalchemy import BigInteger,DateTime,Integer,JSON,Numeric,String,Text,UniqueConstraint,select,update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped,mapped_column
from trading.database import Base

JSON_TYPE=JSON().with_variant(JSONB(),'postgresql')
PK_TYPE=BigInteger().with_variant(Integer(),'sqlite')


class StrategyExecutionState(Base):
    __tablename__='strategy_execution_states'
    __table_args__=(UniqueConstraint('user_id','strategy_key','mode',name='idx_strategy_state_account_key_mode'),)
    id: Mapped[int]=mapped_column(PK_TYPE,primary_key=True)
    user_id: Mapped[int]=mapped_column(BigInteger,nullable=False)
    strategy_key: Mapped[str]=mapped_column(String,nullable=False)
    strategy_version: Mapped[str]=mapped_column(String,nullable=False)
    mode: Mapped[str]=mapped_column(String,nullable=False)
    status: Mapped[str]=mapped_column(String,nullable=False,default='idle')
    checkpoint_revision: Mapped[int]=mapped_column(Integer,nullable=False,default=0)
    last_processed_candle: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    position_trade_id: Mapped[int|None]=mapped_column(BigInteger)
    pending_signal: Mapped[dict]=mapped_column(JSON_TYPE,nullable=False,default=dict)
    configuration: Mapped[dict]=mapped_column(JSON_TYPE,nullable=False,default=dict)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True))


class StrategyOrderIntent(Base):
    __tablename__='strategy_order_intents'
    __table_args__=(UniqueConstraint('user_id','broker_order_id',name='idx_strategy_intent_broker_order'),)
    id: Mapped[int]=mapped_column(PK_TYPE,primary_key=True)
    user_id: Mapped[int]=mapped_column(BigInteger,nullable=False)
    strategy_key: Mapped[str]=mapped_column(String,nullable=False)
    strategy_version: Mapped[str]=mapped_column(String,nullable=False)
    mode: Mapped[str]=mapped_column(String,nullable=False)
    idempotency_key: Mapped[str]=mapped_column(String,unique=True,nullable=False)
    signal_time: Mapped[datetime]=mapped_column(DateTime(timezone=True),nullable=False)
    action: Mapped[str]=mapped_column(String,nullable=False)
    status: Mapped[str]=mapped_column(String,nullable=False,default='reserved')
    client_tag: Mapped[str]=mapped_column(String,unique=True,nullable=False)
    broker_order_id: Mapped[str|None]=mapped_column(String)
    contract: Mapped[dict]=mapped_column(JSON_TYPE,nullable=False)
    quantity: Mapped[int]=mapped_column(Integer,nullable=False)
    filled_quantity: Mapped[int]=mapped_column(Integer,nullable=False,default=0)
    average_price: Mapped[Decimal|None]=mapped_column(Numeric(14,4))
    error_message: Mapped[str|None]=mapped_column(Text)
    applied_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True))


def event_key(user_id,strategy_key,mode,signal_time,action):
    if signal_time.tzinfo is None:
        raise ValueError('Signal time must be timezone aware')
    if mode not in {'paper','live'} or action not in {'entry','exit','roll_entry','roll_exit'}:
        raise ValueError('Invalid mode or intent action')
    # Exclude process/run/version IDs: restarting or renaming a version must
    # not turn the same signal/action into permission for a second order.
    parts=[int(user_id),strategy_key,mode,signal_time.astimezone(timezone.utc).isoformat(),action]
    return hashlib.sha256(json.dumps(parts,separators=(',',':')).encode()).hexdigest()


def snapshot(row):
    return {k:getattr(row,k) for k in ('id','idempotency_key','client_tag','status','broker_order_id',
                                      'quantity','filled_quantity','strategy_version','contract')}


class IntentJournal:
    def __init__(self,session_factory=None):
        if session_factory is None:
            from trading.database import get_db
            session_factory=get_db
        self.session_factory=session_factory

    def load_state(self,*,user_id,strategy_key,mode):
        with self.session_factory() as db:
            row=db.scalar(select(StrategyExecutionState).where(StrategyExecutionState.user_id==user_id,
                StrategyExecutionState.strategy_key==strategy_key,StrategyExecutionState.mode==mode))
            if row is None: return None
            return {k:getattr(row,k) for k in ('strategy_version','status','checkpoint_revision',
                'last_processed_candle','position_trade_id','pending_signal','configuration')}

    def save_checkpoint(self,*,user_id,strategy_key,strategy_version,mode,expected_revision,
                        candle_time,position_trade_id,pending_signal,configuration,status):
        if (mode not in {'paper','live'} or status not in {'idle','position_open','reconcile','halted'}
            or candle_time.tzinfo is None or type(expected_revision) is not int or expected_revision<0):
            raise ValueError('Invalid checkpoint mode, status, time or revision')
        candle_time=candle_time.astimezone(timezone.utc)
        now=datetime.now(timezone.utc)
        with self.session_factory() as db:
            try:
                with db.begin_nested():
                    row=StrategyExecutionState(user_id=user_id,strategy_key=strategy_key,strategy_version=strategy_version,
                        mode=mode,status='idle',checkpoint_revision=0,pending_signal={},configuration={},
                        created_at=now,updated_at=now)
                    db.add(row); db.flush()
            except IntegrityError:
                pass
            row=db.scalar(select(StrategyExecutionState).where(StrategyExecutionState.user_id==user_id,
                StrategyExecutionState.strategy_key==strategy_key,StrategyExecutionState.mode==mode).with_for_update())
            if row.checkpoint_revision!=expected_revision:
                raise RuntimeError('Checkpoint changed in another worker; reload before processing')
            if row.last_processed_candle:
                prior=row.last_processed_candle
                prior=prior.replace(tzinfo=timezone.utc) if prior.tzinfo is None else prior
                if candle_time<prior:
                    raise RuntimeError('Cannot move a signal checkpoint backwards')
            if row.position_trade_id and (row.strategy_version!=strategy_version or row.configuration!=configuration):
                raise RuntimeError('Cannot change strategy/configuration while its position is open')
            row.strategy_version=strategy_version
            row.last_processed_candle=candle_time
            row.position_trade_id=position_trade_id
            row.pending_signal=pending_signal
            row.configuration=configuration
            row.status=status
            row.checkpoint_revision+=1
            row.updated_at=now
            return row.checkpoint_revision

    def reserve(self,*,user_id,strategy_key,strategy_version,mode,signal_time,action,contract,quantity):
        if type(quantity) is not int or quantity<=0 or not contract.get('tradingsymbol'):
            raise ValueError('Positive integer quantity and contract symbol required')
        key=event_key(user_id,strategy_key,mode,signal_time,action)
        now=datetime.now(timezone.utc)
        with self.session_factory() as db:
            try:
                with db.begin_nested():
                    row=StrategyOrderIntent(user_id=user_id,strategy_key=strategy_key,strategy_version=strategy_version,
                        mode=mode,idempotency_key=key,signal_time=signal_time.astimezone(timezone.utc),action=action,status='reserved',
                        client_tag='SI'+key[:18],contract=contract,quantity=quantity,filled_quantity=0,
                        created_at=now,updated_at=now)
                    db.add(row)
                    db.flush()
            except IntegrityError:
                row=db.scalar(select(StrategyOrderIntent).where(StrategyOrderIntent.idempotency_key==key))
                if row is None:
                    raise
            if row.quantity!=quantity or row.contract!=contract or row.strategy_version!=strategy_version:
                raise RuntimeError('Existing signal intent differs; reconcile instead of reserving a replacement')
            result=snapshot(row)
        return result  # context commits BEFORE caller can submit to broker

    def claim_submission(self,key):
        with self.session_factory() as db:
            target=db.scalar(select(StrategyOrderIntent).where(StrategyOrderIntent.idempotency_key==key))
            if target is None: raise RuntimeError('Reserve and commit the intent before submission')
            if db.bind.dialect.name=='postgresql':
                from sqlalchemy import text
                db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))'),
                           {'key':f'intent-submit:{target.user_id}:{target.strategy_key}:{target.mode}'})
            unresolved=db.scalar(select(StrategyOrderIntent.id).where(
                StrategyOrderIntent.user_id==target.user_id,StrategyOrderIntent.strategy_key==target.strategy_key,
                StrategyOrderIntent.mode==target.mode,StrategyOrderIntent.idempotency_key!=key,
                StrategyOrderIntent.status.in_(['submitting','submitted','uncertain'])).limit(1))
            if unresolved is not None:
                raise RuntimeError('Another strategy order is unresolved; reconcile before any new submission')
            result=db.execute(update(StrategyOrderIntent).where(StrategyOrderIntent.idempotency_key==key,
                StrategyOrderIntent.status=='reserved').values(status='submitting',updated_at=datetime.now(timezone.utc)))
            claimed=result.rowcount==1
        return claimed  # committed; a restarted process cannot claim it twice

    def broker_submitted(self,key,order_id):
        if not str(order_id).strip(): raise ValueError('Broker order ID required')
        self._transition(key,{'submitting'},'submitted',broker_order_id=str(order_id))

    def attach_reconciled_order(self,key,orders):
        """Accept only one broker record with matching durable tag and contract."""
        with self.session_factory() as db:
            row=db.scalar(select(StrategyOrderIntent).where(StrategyOrderIntent.idempotency_key==key).with_for_update())
            if row is None or row.status not in {'submitting','submitted','uncertain'}:
                raise RuntimeError('Intent is not awaiting broker reconciliation')
            matches=[o for o in orders if o.get('tag')==row.client_tag]
            if not matches:
                return False  # Absence from today's order book is NOT proof of non-submission.
            if len(matches)!=1:
                raise RuntimeError('Multiple broker orders share intent tag; halt for reconciliation')
            order=matches[0]
            side='BUY' if row.action in {'entry','roll_entry'} else 'SELL'
            if (order.get('tradingsymbol')!=row.contract['tradingsymbol']
                or int(order.get('quantity') or 0)!=row.quantity
                or order.get('transaction_type')!=side
                or order.get('exchange')!=row.contract.get('exchange','NFO')
                or (row.contract.get('product') and order.get('product')!=row.contract['product'])
                or not order.get('order_id')):
                raise RuntimeError('Broker order does not match the immutable intent')
            order_id=str(order['order_id'])
            if row.broker_order_id and row.broker_order_id!=order_id:
                raise RuntimeError('Intent already references a different broker order')
            row.broker_order_id=order_id
            row.status='submitted'
            row.updated_at=datetime.now(timezone.utc)
            return True

    def uncertain(self,key,message):
        self._transition(key,{'submitting','submitted'},'uncertain',error_message=str(message)[:2000])

    def confirmed_fill(self,key,*,filled_quantity,average_price):
        price=Decimal(str(average_price))
        if not price.is_finite() or price<=0:
            raise ValueError('Positive finite confirmed average price required')
        with self.session_factory() as db:
            row=db.scalar(select(StrategyOrderIntent).where(StrategyOrderIntent.idempotency_key==key).with_for_update())
            if row is None or row.status not in {'submitted','uncertain'} or not row.broker_order_id:
                raise RuntimeError('A known broker order must be reconciled before confirming a fill')
            if type(filled_quantity) is not int or not 0<=filled_quantity<=row.quantity:
                raise ValueError('Invalid filled quantity')
            row.filled_quantity=filled_quantity
            row.average_price=price
            row.status='filled' if filled_quantity==row.quantity else 'uncertain'
            row.error_message=None if row.status=='filled' else 'Partial fill: reconcile remaining quantity; never resubmit whole lot'
            row.updated_at=datetime.now(timezone.utc)

    def _transition(self,key,allowed,status,**values):
        with self.session_factory() as db:
            changed=db.execute(update(StrategyOrderIntent).where(StrategyOrderIntent.idempotency_key==key,
                StrategyOrderIntent.status.in_(allowed)).values(status=status,updated_at=datetime.now(timezone.utc),**values))
            if changed.rowcount!=1:
                raise RuntimeError('Intent state changed or missing; reconcile before proceeding')

    def recovery(self,*,user_id,strategy_key,mode):
        with self.session_factory() as db:
            rows=db.scalars(select(StrategyOrderIntent).where(StrategyOrderIntent.user_id==user_id,
                StrategyOrderIntent.strategy_key==strategy_key,StrategyOrderIntent.mode==mode,
                StrategyOrderIntent.status.in_(['submitting','submitted','uncertain'])).order_by(StrategyOrderIntent.id))
            return [snapshot(row) for row in rows]
