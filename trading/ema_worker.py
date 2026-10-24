"""Signal-to-intent coordinator; execution effects are supplied by the selected adapter."""
from datetime import datetime,time,timedelta
from strategies.nifty_daily_ema_monthly import sessions_to_expiry
from trading.ema_execution import aware,ReconciliationRequired


class Worker:
    def __init__(self,executor,side):
        if side not in {'CE','PE'}:raise ValueError('Invalid strategy side')
        self.executor=executor;self.store=executor.store;self.side=side

    def fresh(self,snapshot,now):
        return snapshot['eligible_after']<=now<=snapshot['deadline']

    def step(self,snapshot,now):
        state=self.store.state()
        if not state:
            seed=snapshot['time']-timedelta(microseconds=1) if self.fresh(snapshot,now) else snapshot['time']
            self.store.initialize(seed)
        self.executor.recover(now)
        if self.store.state()['status'] in {'halted','reconcile'}:
            raise ReconciliationRequired('Checkpoint requires manual reconciliation before resuming')
        position=self.store.position()
        self.executor.broker.verify_owned(position)
        if position and position['expiry']<now.date():
            raise ReconciliationRequired('Recorded option expired; manual settlement reconciliation required')
        state=self.store.state()
        # An acknowledged order is always recovered before a pending signal can be changed.
        pending=state['pending_signal']
        if pending and pending['action'] in {'entry','roll_entry'}:
            deadline=datetime.fromisoformat(pending['deadline'])
            if not snapshot['trend_valid'] or now>deadline:
                self.store.cancel_reserved(pending,now,'stale_entry_or_changed_trend')
        self._drain(now)
        state=self.store.state();position=self.store.position()
        new=snapshot['time']>aware(state['last_processed_candle'])
        pending={}
        if new:
            if position and (snapshot['exit'] or not snapshot['trend_valid']):
                pending=self._signal('exit',snapshot['time'],snapshot['deadline'])
            elif not position and snapshot['enter'] and self.fresh(snapshot,now):
                pending=self._signal('entry',snapshot['time'],snapshot['deadline'])
            self.store.checkpoint(snapshot['time'],pending)
            details={'side':self.side if snapshot['trend_valid'] else None,'trend':'bullish' if snapshot['bullish'] else 'bearish',
                     'decision':pending.get('action','hold' if position else 'wait'), 'signal':pending.get('action','hold' if position else 'wait'),
                     'completed_date':str(snapshot['time'].date()),'fast_ema':snapshot['fast'],'slow_ema':snapshot['slow'],
                     'mode':self.store.mode,'real_orders_enabled':self.store.mode=='live'}
            action='DAILY_EMA_SIGNAL' if self.side=='CE' else 'BEARISH_EMA_SIGNAL'
            self.store.event(action,'completed_candle',details,now)
            self._drain(now)
        # Roll only an owned lot, and only after processing the latest trend/exit.
        position=self.store.position()
        if (position and snapshot.get('is_latest',True) and snapshot['trend_valid'] and not self.store.state()['pending_signal']
            and sessions_to_expiry(now.date(),position['expiry'],self.executor.trading_day)<=2):
            roll_time=now.replace(hour=9,minute=15,second=0,microsecond=0)
            deadline=now.replace(hour=15,minute=29,second=59,microsecond=0)
            signal=self._signal('roll_exit',roll_time,deadline)
            signal['followup']=self._signal('roll_entry',roll_time,deadline)
            self.store.checkpoint(aware(self.store.state()['last_processed_candle']),signal)
            self._drain(now)
        return self.store.state()

    def _signal(self,action,time,deadline):
        return dict(action=action,time=time.isoformat(),deadline=deadline.isoformat(),side=self.side)

    def _drain(self,now):
        # Two iterations allow confirmed roll exit -> replacement entry. Each leg
        # is committed separately; a restart resumes only the unfinished leg.
        for _ in range(2):
            if not self.store.state()['pending_signal']:break
            self.executor.execute_pending(now)
