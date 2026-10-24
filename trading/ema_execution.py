"""Restart-recoverable EMA option execution. Broker effects require an explicit adapter."""
from datetime import datetime,timezone,date
from decimal import Decimal
from sqlalchemy import select,func

from trading.strategy_order_intents import IntentJournal,StrategyExecutionState,StrategyOrderIntent,event_key
from trading.strategy_records import StrategyRun,StrategyTrade,StrategyEvent
from strategies.nifty_daily_ema_monthly import Settings,quote_fill,sessions_to_expiry

FINAL={'COMPLETE','REJECTED','CANCELLED'}


def aware(t):
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t


class ReconciliationRequired(RuntimeError): pass


class Store:
    def __init__(self,factory,user_id,key,version,mode,configuration):
        self.factory,self.user_id,self.key,self.version,self.mode,self.configuration=factory,user_id,key,version,mode,configuration
        self.journal=IntentJournal(factory)
        self.run_id=None

    def state(self):
        return self.journal.load_state(user_id=self.user_id,strategy_key=self.key,mode=self.mode)

    def start(self,now):
        with self.factory() as db:
            # Only call while holding the account/strategy process lock.
            prior=db.scalars(select(StrategyRun).where(StrategyRun.user_id==self.user_id,StrategyRun.strategy_key==self.key,
                StrategyRun.mode==self.mode,StrategyRun.status=='running'))
            for r in prior:
                r.status='stopped';r.ended_at=now;r.error_message='Recovered after prior process stopped'
            db.flush()
            r=StrategyRun(user_id=self.user_id,strategy_key=self.key,strategy_version=self.version,mode=self.mode,
                status='running',started_at=now,heartbeat_at=now,configuration=self.configuration)
            db.add(r);db.flush();self.run_id=r.id

    def heartbeat(self,now,status=None,message=None):
        with self.factory() as db:
            r=db.get(StrategyRun,self.run_id)
            r.heartbeat_at=r.updated_at=now
            if status:
                r.status=status;r.ended_at=now;r.error_message=message

    def event(self,action,reason,details,now):
        with self.factory() as db:
            db.add(StrategyEvent(strategy_run_id=self.run_id,occurred_at=now,action=action,reason=reason,details=details))

    def initialize(self,candle_time):
        state=self.state()
        if state:
            if state['strategy_version']!=self.version or state['configuration']!=self.configuration:
                raise ReconciliationRequired('Checkpoint version/configuration differs; explicit migration required')
            return state
        with self.factory() as db:
            old=list(db.scalars(select(StrategyTrade).where(StrategyTrade.user_id==self.user_id,
                StrategyTrade.strategy_key==self.key,StrategyTrade.mode==self.mode,
                StrategyTrade.status.notin_(['closed','rejected']))))
            if old:
                raise ReconciliationRequired('Existing trade without checkpoint; reconcile ownership before initialization')
        self.journal.save_checkpoint(user_id=self.user_id,strategy_key=self.key,strategy_version=self.version,mode=self.mode,
            expected_revision=0,candle_time=candle_time,position_trade_id=None,pending_signal={},configuration=self.configuration,status='idle')
        return self.state()

    def checkpoint(self,candle_time,pending):
        state=self.state()
        self.journal.save_checkpoint(user_id=self.user_id,strategy_key=self.key,strategy_version=self.version,mode=self.mode,
            expected_revision=state['checkpoint_revision'],candle_time=candle_time,position_trade_id=state['position_trade_id'],
            pending_signal=pending,configuration=self.configuration,status='position_open' if state['position_trade_id'] else 'idle')

    def position(self):
        state=self.state()
        if not state or not state['position_trade_id']:return None
        with self.factory() as db:
            t=db.get(StrategyTrade,state['position_trade_id'])
            if t is None or (t.user_id,t.strategy_key,t.mode,t.status)!=(self.user_id,self.key,self.mode,'open'):
                raise ReconciliationRequired('Checkpoint position ownership/status mismatch')
            return dict(id=t.id,symbol=t.option_symbol,expiry=t.option_expiry,side=t.side,quantity=t.quantity,
                        strike=str(t.option_strike),entry_premium=str(t.entry_premium))

    def cancel_reserved(self,pending,now,reason):
        key=event_key(self.user_id,self.key,self.mode,datetime.fromisoformat(pending['time']),pending['action'])
        with self.factory() as db:
            intent=db.scalar(select(StrategyOrderIntent).where(StrategyOrderIntent.idempotency_key==key).with_for_update())
            if intent and intent.status not in {'reserved','rejected','cancelled'}:
                raise ReconciliationRequired('Cannot discard an order with a possible broker effect')
            if intent and intent.status=='reserved':
                intent.status='cancelled';intent.error_message=reason;intent.updated_at=now
            state=db.scalar(select(StrategyExecutionState).where(StrategyExecutionState.user_id==self.user_id,
                StrategyExecutionState.strategy_key==self.key,StrategyExecutionState.mode==self.mode).with_for_update())
            state.pending_signal={};state.checkpoint_revision+=1;state.updated_at=now

    def intent(self,key):
        with self.factory() as db:
            row=db.scalar(select(StrategyOrderIntent).where(StrategyOrderIntent.idempotency_key==key,
                StrategyOrderIntent.user_id==self.user_id,StrategyOrderIntent.strategy_key==self.key,StrategyOrderIntent.mode==self.mode))
            if not row:return None
            return {k:getattr(row,k) for k in ('id','status','client_tag','broker_order_id','contract','quantity','action',
                                               'signal_time','average_price','filled_quantity','applied_at','idempotency_key')}

    def recoverable(self):
        with self.factory() as db:
            rows=db.scalars(select(StrategyOrderIntent).where(StrategyOrderIntent.user_id==self.user_id,
                StrategyOrderIntent.strategy_key==self.key,StrategyOrderIntent.mode==self.mode,
                StrategyOrderIntent.status.in_(['submitting','submitted','uncertain','filled']),StrategyOrderIntent.applied_at.is_(None)))
            return [r.idempotency_key for r in rows]

    def apply_fill(self,key,now):
        """Atomically apply confirmed fill, owned trade and checkpoint exactly once."""
        with self.factory() as db:
            intent=db.scalar(select(StrategyOrderIntent).where(StrategyOrderIntent.idempotency_key==key).with_for_update())
            state=db.scalar(select(StrategyExecutionState).where(StrategyExecutionState.user_id==self.user_id,
                StrategyExecutionState.strategy_key==self.key,StrategyExecutionState.mode==self.mode).with_for_update())
            if intent.applied_at:return
            if intent.status!='filled' or intent.filled_quantity!=intent.quantity or not intent.average_price:
                raise ReconciliationRequired('Only a confirmed full fill can change position ownership')
            if (intent.user_id,intent.strategy_key,intent.mode)!=(self.user_id,self.key,self.mode):
                raise ReconciliationRequired('Intent ownership mismatch')
            c=intent.contract
            index=Decimal(c['signal_index'])
            if intent.action in {'entry','roll_entry'}:
                if state.position_trade_id:
                    raise ReconciliationRequired('Entry fill found while strategy already owns a position')
                other=db.scalar(select(StrategyTrade.id).where(StrategyTrade.user_id==self.user_id,StrategyTrade.strategy_key==self.key,
                    StrategyTrade.mode==self.mode,StrategyTrade.status.notin_(['closed','rejected'])).limit(1))
                if other:raise ReconciliationRequired('Another unresolved strategy trade exists')
                count=db.scalar(select(func.count()).select_from(StrategyTrade).where(StrategyTrade.strategy_run_id==self.run_id,
                    StrategyTrade.trading_date==now.date())) or 0
                t=StrategyTrade(strategy_run_id=self.run_id,user_id=self.user_id,strategy_key=self.key,strategy_version=self.version,
                    mode=self.mode,trading_date=now.date(),trade_number=count+1,status='open',side=c['side'],
                    option_symbol=c['tradingsymbol'],option_expiry=date.fromisoformat(c['expiry']),option_strike=Decimal(c['strike']),
                    lot_count=1,quantity=intent.quantity,entry_signal_time=intent.signal_time,entry_confirmed_at=now,
                    entry_reason='monthly_roll' if intent.action=='roll_entry' else 'ema_crossover',entry_index=index,
                    entry_premium=intent.average_price,entry_order_id=intent.broker_order_id,initial_stop=None,current_stop=None,target=None)
                db.add(t);db.flush();state.position_trade_id=t.id
                state.pending_signal={}
            else:
                t=db.get(StrategyTrade,c['trade_id'],with_for_update=True)
                if t is None or t.id!=state.position_trade_id or (t.user_id,t.strategy_key,t.mode,t.status)!=(self.user_id,self.key,self.mode,'open'):
                    raise ReconciliationRequired('Exit cannot claim a missing/manual/closed position')
                t.exit_order_id=intent.broker_order_id;t.exit_premium=intent.average_price
                t.exit_signal_time=intent.signal_time;t.exit_confirmed_at=now;t.exit_index=index
                t.exit_reason='monthly_roll' if intent.action=='roll_exit' else 'ema_reversal'
                t.gross_option_pnl=(t.exit_premium-t.entry_premium)*t.quantity
                t.index_points=(index-t.entry_index)*(1 if t.side=='CE' else -1)
                t.status='closed';t.updated_at=now
                state.position_trade_id=None
                follow=state.pending_signal.get('followup') if intent.action=='roll_exit' else None
                state.pending_signal=follow or {}
            state.status='position_open' if state.position_trade_id else 'idle'
            state.checkpoint_revision+=1;state.updated_at=now
            intent.applied_at=now;intent.updated_at=now
            db.add(StrategyEvent(strategy_run_id=self.run_id,strategy_trade_id=t.id,occurred_at=now,
                action=('BUY_' if intent.action in {'entry','roll_entry'} else 'EXIT_')+t.side,reason=intent.action,
                index_price=index,details={'intent_key':key,'broker_order_id':intent.broker_order_id,'mode':self.mode,
                'premium':str(intent.average_price),'quantity':intent.quantity}))

    def reject(self,key,message,now):
        with self.factory() as db:
            intent=db.scalar(select(StrategyOrderIntent).where(StrategyOrderIntent.idempotency_key==key).with_for_update())
            if intent.status not in {'submitting','submitted','uncertain'}:raise ReconciliationRequired('Unexpected rejection state')
            intent.status='rejected';intent.error_message=message;intent.updated_at=now
            state=db.scalar(select(StrategyExecutionState).where(StrategyExecutionState.user_id==self.user_id,
                StrategyExecutionState.strategy_key==self.key,StrategyExecutionState.mode==self.mode).with_for_update())
            state.pending_signal={};state.checkpoint_revision+=1;state.updated_at=now
            if intent.action in {'exit','roll_exit'}:state.status='halted'
            db.add(StrategyEvent(strategy_run_id=self.run_id,occurred_at=now,action='ORDER_REJECTED',
                reason=message,details={'intent_key':key,'mode':self.mode}))


class Executor:
    def __init__(self,store,broker,instruments,trading_day):
        self.store,self.broker,self.instruments,self.trading_day=store,broker,instruments,trading_day
        self.settings=Settings(Decimal('30000'),2)

    def recover(self,now):
        for key in self.store.recoverable(): self.reconcile(key,now)

    def reconcile(self,key,now):
        intent=self.store.intent(key)
        if intent['status']=='filled':
            self.store.apply_fill(key,now);return
        if intent['broker_order_id']:
            order=self.broker.order(intent['broker_order_id'])
            if not order:raise ReconciliationRequired('Broker no longer has pending order history; reconcile manually')
            candidates=[order]
        else:candidates=self.broker.orders()
        if not self.store.journal.attach_reconciled_order(key,candidates):
            raise ReconciliationRequired('Submission outcome unknown; no matching broker record, no automatic retry')
        order=next(o for o in candidates if o.get('tag')==intent['client_tag'])
        filled=int(order.get('filled_quantity') or 0)
        if order['status']=='COMPLETE' and filled==intent['quantity']:
            self.store.journal.confirmed_fill(key,filled_quantity=filled,average_price=order['average_price'])
            self.store.apply_fill(key,now)
        elif order['status'] in {'REJECTED','CANCELLED'} and filled==0:
            self.store.reject(key,str(order.get('status_message') or order['status']),now)
            raise ReconciliationRequired('Broker rejected or cancelled the order; inspect before resuming')
        else:
            self.store.journal.uncertain(key,'Pending or partial broker fill; reconcile, never resubmit')
            raise ReconciliationRequired('Order is not fully filled; strategy halted for reconciliation')

    def select_contract(self,side,now):
        spot=self.broker.spot(now)
        expiries=sorted({i['expiry'] for i in self.instruments if i.get('name')=='NIFTY' and i.get('instrument_type')=='FUT'
            and i.get('expiry') and i['expiry']>=now.date()
            and sessions_to_expiry(now.date(),i['expiry'],self.trading_day)>2})
        for expiry in expiries:
            chain=[i for i in self.instruments if i.get('name')=='NIFTY' and i.get('instrument_type')==side and i.get('expiry')==expiry]
            if not chain:continue
            c=min(chain,key=lambda i:(abs(Decimal(str(i['strike']))-spot),Decimal(str(i['strike']))))
            quantity=int(c['lot_size'])
            if quantity<=0:raise ValueError('Invalid lot size')
            contract={'tradingsymbol':c['tradingsymbol'],'expiry':str(expiry),'strike':str(c['strike']),
                      'side':side,'lot_size':quantity,'tick_size':str(c.get('tick_size') or '0.05'),'exchange':'NFO','product':'NRML','signal_index':str(spot)}
            self.broker.check_entry(contract,quantity,now,self.settings)
            return contract,quantity
        raise ValueError('No eligible monthly contract; entry blocked')

    def execute_pending(self,now):
        state=self.store.state();pending=state['pending_signal']
        if not pending:return
        signal_time=datetime.fromisoformat(pending['time'])
        action=pending['action']
        key=event_key(self.store.user_id,self.store.key,self.store.mode,signal_time,action)
        intent=self.store.intent(key)
        if intent and intent['status']=='filled':self.store.apply_fill(key,now);return
        if intent and intent['status'] in {'submitting','submitted','uncertain'}:self.reconcile(key,now);return
        if intent and intent['status'] in {'rejected','cancelled'}:
            self.store.cancel_reserved(pending,now,'terminal_intent');return
        if not intent:
            if action in {'entry','roll_entry'}:
                contract,quantity=self.select_contract(pending['side'],now)
            else:
                position=self.store.position()
                if not position:raise ReconciliationRequired('Exit signal without owned position')
                if position['expiry']<now.date():raise ReconciliationRequired('Owned contract expired; reconcile settlement')
                contract=dict(tradingsymbol=position['symbol'],expiry=str(position['expiry']),strike=position['strike'],
                    side=position['side'],exchange='NFO',product='NRML',trade_id=position['id'],signal_index=str(self.broker.spot(now)))
                quantity=position['quantity']
                self.broker.check_exit(contract,quantity,now,self.settings)
            self.store.journal.reserve(user_id=self.store.user_id,strategy_key=self.store.key,strategy_version=self.store.version,
                mode=self.store.mode,signal_time=signal_time,action=action,contract=contract,quantity=quantity)
            intent=self.store.intent(key)
        buying=action in {'entry','roll_entry'}
        if buying:self.broker.check_entry(intent['contract'],intent['quantity'],now,self.settings)
        else:self.broker.check_exit(intent['contract'],intent['quantity'],now,self.settings)
        if not self.store.journal.claim_submission(key):raise ReconciliationRequired('Intent was already claimed')
        try:
            order_id=self.broker.submit(intent['contract'],'BUY' if buying else 'SELL',intent['quantity'],intent['client_tag'],now)
            self.store.journal.broker_submitted(key,order_id)
        except BaseException:
            self.store.journal.uncertain(key,'Submission interrupted or acknowledgement lost; reconcile before retry')
            raise
        self.reconcile(key,now)
