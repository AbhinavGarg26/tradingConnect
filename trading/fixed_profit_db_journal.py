"""Database-first fixed-profit journal. Restarts fail closed; never replay orders."""
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import json
from sqlalchemy import select, or_, text
from strategies.fixed_profit_filtered import CONFIG, CONFIG_HASH, KEY, VERSION
from trading.strategy_records import StrategyRun, StrategyTrade, StrategyEvent
from trading.database import get_db, engine


class DatabaseSessionJournal:
    def __init__(self, directory, user_id, today, *, live, factory=get_db, db_engine=engine):
        self.factory,self.user_id,self.today=factory,user_id,today
        self.mode='live' if live else 'paper'
        self.trade_id=None
        # Refuse to abandon legacy local reservations during the DB migration.
        for path in Path(directory).glob(f'{user_id}-*-{self.mode}.json'):
            legacy=json.loads(path.read_text())
            actions=[e['action'] for e in legacy.get('events',[])]
            if legacy.get('date')==str(today) or ('ENTRY_INTENT' in actions and 'EXIT_FILLED' not in actions):
                raise RuntimeError('Legacy local journal requires reconciliation before database startup')
        self.connection=db_engine.connect()
        self.lock_key=f'fixed-profit:{user_id}'
        try:
            if not self.connection.execute(text('SELECT pg_try_advisory_lock(hashtextextended(:key,0))'),{'key':self.lock_key}).scalar():
                raise RuntimeError('Another fixed-profit worker owns this account')
            with factory() as db:
                prior=db.scalar(select(StrategyRun.id).where(StrategyRun.user_id==user_id,
                    StrategyRun.strategy_key==KEY,StrategyRun.mode==self.mode,
                    or_(StrategyRun.configuration['session_date'].as_string()==str(today),
                        StrategyRun.status.in_(['running','failed','stopped']))).limit(1))
                unresolved=db.scalar(select(StrategyTrade.id).where(StrategyTrade.user_id==user_id,
                    StrategyTrade.strategy_key==KEY,StrategyTrade.mode==self.mode,
                    StrategyTrade.status!='closed').limit(1))
                if prior or unresolved:raise RuntimeError('Prior session or unresolved fixed-profit state; reconcile before restart')
                now=datetime.now(timezone.utc)
                run=StrategyRun(user_id=user_id,strategy_key=KEY,strategy_version=VERSION,mode=self.mode,
                    status='running',started_at=now,heartbeat_at=now,
                    configuration=dict(CONFIG,session_date=str(today),config_hash=CONFIG_HASH,journal='database'))
                db.add(run);db.flush();self.run_id=run.id
        except BaseException:
            self.close();raise

    def event(self,action,**details):
        now=datetime.now(timezone.utc)
        with self.factory() as db:
            if action=='ENTRY_INTENT':
                if self.trade_id is not None:raise RuntimeError('Entry already reserved')
                trade=StrategyTrade(strategy_run_id=self.run_id,user_id=self.user_id,strategy_key=KEY,
                    strategy_version=VERSION,mode=self.mode,trading_date=self.today,trade_number=1,
                    status='entry_pending',side=details['side'],option_symbol=details['symbol'],
                    option_expiry=date.fromisoformat(details['expiry']),option_strike=Decimal(details['strike']),
                    lot_count=1,quantity=details['quantity'],entry_signal_time=now,entry_reason='filtered_breakout_retest',
                    entry_index=Decimal(details['index']),reference_high=Decimal(details['reference_high']),
                    reference_low=Decimal(details['reference_low']),initial_stop=None,current_stop=None,target=None)
                db.add(trade);db.flush();self.trade_id=trade.id
            trade=db.get(StrategyTrade,self.trade_id,with_for_update=True) if self.trade_id else None
            if trade:
                if action=='ORDER_SUBMITTED':
                    if trade.status=='entry_pending':trade.entry_order_id=details['order_id']
                    elif trade.status=='exit_pending':trade.exit_order_id=details['order_id']
                elif action=='ENTRY_FILLED':
                    if trade.status!='entry_pending':raise RuntimeError('Entry reservation changed')
                    trade.status='open';trade.entry_premium=Decimal(details['premium'])
                    trade.entry_index=Decimal(details['index']);trade.initial_stop=trade.current_stop=Decimal(details['stop'])
                    trade.target=Decimal(details['target']);trade.entry_confirmed_at=now
                    trade.entry_order_id=details.get('order_id')
                elif action=='EXIT_INTENT':
                    if trade.status!='open':raise RuntimeError('Exit ownership changed')
                    trade.status='exit_pending';trade.exit_signal_time=now;trade.exit_reason=details['reason']
                    trade.exit_index=Decimal(details['index'])
                elif action=='EXIT_FILLED':
                    if trade.status!='exit_pending':raise RuntimeError('Exit reservation changed')
                    trade.status='closed';trade.exit_premium=Decimal(details['premium']);trade.exit_confirmed_at=now
                    trade.exit_order_id=details.get('order_id')
                    trade.gross_option_pnl=(trade.exit_premium-trade.entry_premium)*trade.quantity
                    trade.index_points=(1 if trade.side=='CE' else -1)*(trade.exit_index-trade.entry_index)
                elif action=='RECONCILE_REQUIRED':trade.status='reconcile'
                trade.updated_at=now
            db.add(StrategyEvent(strategy_run_id=self.run_id,strategy_trade_id=self.trade_id,occurred_at=now,
                action=action,reason=str(details.get('reason',action.lower())),details=details))

    def heartbeat(self):
        with self.factory() as db:
            run=db.get(StrategyRun,self.run_id);run.heartbeat_at=run.updated_at=datetime.now(timezone.utc)

    def finish(self,status):
        with self.factory() as db:
            run=db.get(StrategyRun,self.run_id);run.status=status
            run.ended_at=run.heartbeat_at=run.updated_at=datetime.now(timezone.utc)

    def close(self):
        if not self.connection.closed:
            try:
                self.connection.rollback()
                self.connection.execute(text('SELECT pg_advisory_unlock(hashtextextended(:key,0))'),{'key':self.lock_key})
                self.connection.commit()
            except BaseException:
                self.connection.invalidate()
                raise
            finally:self.connection.close()
