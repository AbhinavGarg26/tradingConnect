"""User-operated EMA runner. Paper by default. --check is read-only. No auto-activation."""
import argparse
from contextlib import contextmanager
from datetime import datetime,time
from decimal import Decimal
import hashlib
import json
import logging
import time as retry_clock
from zoneinfo import ZoneInfo

IST=ZoneInfo('Asia/Kolkata')
log=logging.getLogger(__name__)


@contextmanager
def process_lock(engine,user_id,key,mode):
    from sqlalchemy import text
    with engine.connect() as connection:
        scope={'key':f'ema-worker:{user_id}:{key}:{mode}'}
        locked=connection.execute(text('SELECT pg_try_advisory_lock(hashtextextended(:key,0))'),scope).scalar()
        if not locked:raise RuntimeError('Another process owns this account/strategy/mode')
        try:yield
        finally:
            try:
                connection.rollback()
                connection.execute(text('SELECT pg_advisory_unlock(hashtextextended(:key,0))'),scope)
                connection.commit()
            except BaseException:
                connection.invalidate();raise


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--strategy',choices=('daily_calls','bearish_puts'),required=True)
    p.add_argument('--user-id',type=int,required=True)
    p.add_argument('--mode',choices=('paper','live'),default='paper')
    p.add_argument('--acknowledge-premium-risk',action='store_true',help='Required only for user-selected live mode: no fixed protective stop; premium may be lost')
    p.add_argument('--check',action='store_true')
    p.add_argument('--poll-seconds',type=int,default=20)
    p.add_argument('--skip-incomplete-days',action='store_true')
    args=p.parse_args()
    if args.mode=='live' and not args.check and not args.acknowledge_premium_risk:
        p.error('Live mode requires explicit --acknowledge-premium-risk; no fixed stop is implemented')
    if args.poll_seconds<5:p.error('Poll interval must be at least five seconds')
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    from nifty_15m_env import load_nifty_environment
    load_nifty_environment()
    from trading.database import get_db,engine
    from trading.user_token import fetch_user_token
    from trading.ema_execution import Store,Executor
    from trading.ema_broker import KiteBroker,PaperBroker
    from trading.ema_worker import Worker
    from trading.ema_feed import Feed,Calendar
    from sqlalchemy import inspect
    from strategies.nifty_daily_ema_monthly import KEY as CALL_KEY,VERSION as CALL_VERSION
    from strategies.nifty_bearish_15m import KEY as PUT_KEY,VERSION as PUT_VERSION,BASELINE
    now=datetime.now(IST)
    calendar=Calendar(get_db)
    if not args.check and (not calendar(now.date()) or not time(9,14)<=now.time()<time(15,30)):
        log.info('No execution outside the regular market session');return
    inspector=inspect(engine)
    for table in ('strategy_execution_states','strategy_order_intents','strategy_runs','strategy_trades','strategy_events'):
        if not inspector.has_table(table):raise RuntimeError('Apply Rails migrations before running EMA services')
    if 'applied_at' not in {c['name'] for c in inspector.get_columns('strategy_order_intents')}:
        raise RuntimeError('Apply the confirmed-fill application-marker migration')
    columns={c['name']:c for c in inspector.get_columns('strategy_trades')}
    if any(not columns[k]['nullable'] for k in ('initial_stop','current_stop','target')):
        raise RuntimeError('Nullable stop/target migrations are required')
    kite,user_id=fetch_user_token(log,user_id=args.user_id)
    kite.profile()  # Read-only session validation. No order endpoint in --check.
    key,version,side=(CALL_KEY,CALL_VERSION,'CE') if args.strategy=='daily_calls' else (PUT_KEY,PUT_VERSION,'PE')
    config=dict(kind=args.strategy,side=side,fast=20 if side=='CE' else 9,slow=50 if side=='CE' else 21,
                lot_count=1,max_premium='30000',expiry='monthly',roll_sessions=2,product='NRML',
                fixed_stop=None,target=None,skip_incomplete_days=args.skip_incomplete_days,
                actual_option_quotes=True)
    if side=='PE':config['frozen_baseline_sha256']=hashlib.sha256(BASELINE.read_bytes()).hexdigest()
    store=Store(get_db,user_id,key,version,args.mode,config)
    feed=Feed(kite,get_db,args.strategy,calendar,now,skip_incomplete_days=args.skip_incomplete_days)
    if args.check:
        state=store.state();latest=feed.snapshots(datetime.now(IST))[-1]
        result=dict(read_only=True,live_activation_performed=False,runner_integration='implemented',
                    strategy=key,version=version,mode=args.mode,checkpoint=state,
                    recoverable_intents=store.recoverable(),latest_signal=latest,excluded_incomplete_days=feed.excluded)
        print(json.dumps(result,indent=2,default=str));return
    with process_lock(engine,user_id,key,args.mode):
        store.start(datetime.now(IST))
        broker=KiteBroker(kite,store) if args.mode=='live' else PaperBroker(kite,store)
        executor=Executor(store,broker,kite.instruments('NFO'),calendar)
        worker=Worker(executor,side)
        try:
            while True:
                now=datetime.now(IST)
                if now.date()!=feed.day or now.time()>=time(15,30):
                    store.heartbeat(now,'completed');return
                if now.time()<time(9,15):
                    store.heartbeat(now);retry_clock.sleep(args.poll_seconds);continue
                state=store.state()
                snapshots=feed.snapshots(now,state['last_processed_candle'] if state else None)
                for index,snapshot in enumerate(snapshots):
                    snapshot['is_latest']=index==len(snapshots)-1
                    worker.step(snapshot,datetime.now(IST))
                store.heartbeat(datetime.now(IST))
                retry_clock.sleep(args.poll_seconds)
        except BaseException as exc:
            store.heartbeat(datetime.now(IST),'stopped' if isinstance(exc,KeyboardInterrupt) else 'failed',str(exc)[:2000])
            raise


if __name__=='__main__':
    main()
