"""Frozen 30m V3 live runner. One lot, current expiry, +/-2 listed ATM strikes.

Default is paper. Explicit --live and --not-before gate real entries. This
process must stay running: index stops/reference exits use completed 1m closes.
"""
from __future__ import annotations
import argparse
import fcntl
import logging
import time
from datetime import date, datetime, time as clock_time, timedelta
from decimal import Decimal

from strategies.nifty_15m_breakout import Bar
from strategies.nifty_30m_reference import IST, Nifty30mReference, ReferenceSignal
from strategies.nifty_30m_reference_first_pe import Nifty30mReferenceFirstPE
from nifty_15m_env import load_nifty_environment
load_nifty_environment()

from trading.reference_live_execution import ReferenceExecutor, TAG, FINAL, fresh_price

log=logging.getLogger(__name__)


def startup_allowed(now, not_before):
    return (now.date()>=not_before and now.weekday()<5
            and clock_time(9,15)<=now.time()<clock_time(9,45))


def completed_bars(records,now):
    bars=[]
    for row in records:
        stamp=row['date']
        if stamp.tzinfo is None:stamp=stamp.replace(tzinfo=IST)
        stamp=stamp.astimezone(IST)
        if stamp.date()!=now.date():raise ValueError('Historical candle from wrong session')
        if stamp+timedelta(minutes=1,seconds=2)<=now:
            bars.append(Bar(stamp,*(Decimal(str(row[k])) for k in ('open','high','low','close'))))
    return bars


def configured_engine(preset):
    if preset == 'pe1-v1':
        return Nifty30mReferenceFirstPE(stop_points=25,max_entries=1,buffer_percent=5)
    if preset == 'v3':
        return Nifty30mReference(stop_points=25,max_entries=3,buffer_percent=5)
    raise ValueError('Unknown 30m strategy preset')


def validate_execution_mode(preset, live):
    if preset not in {'v3','pe1-v1'}:
        raise ValueError('Unknown 30m strategy preset')


def run_session(kite,user_id,*,live,poll_seconds=3,monitor=None,now_fn=None,sleep_fn=None,preset='v3'):
    validate_execution_mode(preset,live)
    from trading.reference_live_records import ReferenceRecorder
    now_fn=now_fn or (lambda:datetime.now(IST));sleep_fn=sleep_fn or time.sleep
    today=now_fn().date()
    instruments=kite.instruments('NFO')
    nifty=next((i for i in kite.instruments('NSE') if i['tradingsymbol']=='NIFTY 50'),None)
    if not nifty:raise RuntimeError('Nifty index instrument unavailable')
    if live and any(o.get('tag')==TAG and o.get('status') not in FINAL for o in kite.orders()):
        raise RuntimeError('Unresolved V3 broker order; reconcile before startup')
    engine=configured_engine(preset)
    journal=ReferenceRecorder(user_id,today,live=live,version=preset,max_entries=engine.max_entries)
    executor_class=ReferenceExecutor
    if preset=='pe1-v1':
        from trading.reference_protected_execution import ProtectedReferenceExecutor
        executor_class=ProtectedReferenceExecutor
    executor=executor_class(kite,instruments,journal,live=live,now_fn=now_fn)
    last=None
    try:
        while True:
            now=now_fn()
            if now.date()!=today:raise RuntimeError('Session date changed; reconcile before restart')
            if preset=='pe1-v1':
                if executor.synchronize(engine):
                    journal.heartbeat();sleep_fn(poll_seconds);continue
                if engine.pending and engine.pending.action.startswith('EXIT_'):
                    executor.handle(engine.pending,engine)
                if engine.entry_count>=engine.max_entries and executor.position is None:
                    journal.finish('completed');return
            # Same 15:15 square-off, also effective if historical candles become unavailable.
            if now.time()>=clock_time(15,15,2) and executor.position and engine.pending is None:
                spot=fresh_price(kite,'NSE:NIFTY 50',now)
                signal=ReferenceSignal('EXIT_'+engine.position,now,spot,'session_end',
                                       engine.ce_reference,engine.pe_reference,engine.stop)
                engine.pending=signal
                executor.handle(signal,engine)
            if now.time()>=clock_time(15,16) and executor.position is None:
                journal.finish('completed');return
            records=kite.historical_data(nifty['instrument_token'],f'{today} 09:15:00',
                                         now.strftime('%Y-%m-%d %H:%M:%S'),'minute')
            for bar in completed_bars(records,now):
                if last and bar.time<=last:continue
                signal=engine.on_bar(bar);last=bar.time
                if preset=='pe1-v1' and engine.first_signal_side=='CE':
                    journal.event('SESSION_SKIPPED','first_signal_ce')
                    journal.finish('completed');return
                if engine.reference_updates and engine.reference_updates[-1]['time']==bar.time+timedelta(minutes=1):
                    journal.reference_update(engine.reference_updates[-1])
                if engine.count==30 and not engine.opening_eligible:
                    journal.event('SESSION_SKIPPED','opening_body_below_20',
                                  details={'opening_body':str(engine.opening_body)})
                    journal.finish('completed');return
                if signal:
                    if signal.action.startswith('BUY_') and (now-signal.time).total_seconds()>90:
                        journal.event('ENTRY_SKIPPED','stale_signal',signal=signal)
                        engine.reject_signal();continue
                    log.info('%s index=%s reason=%s CEref=%s PEref=%s',signal.action,
                             signal.index_price,signal.reason,signal.ce_reference,signal.pe_reference)
                    executor.handle(signal,engine)
                    if monitor:
                        state='paper' if executor.position and executor.position.paper else 'live' if executor.position else 'closed'
                        monitor.success(f'{signal.action} {state}; {preset} entries {engine.entry_count}/{engine.max_entries}')
                if engine.entry_count>=engine.max_entries and executor.position is None:
                    journal.finish('completed');return
            journal.heartbeat()
            sleep_fn(poll_seconds)
    except BaseException as exc:
        journal.uncertain(str(exc) or type(exc).__name__)
        journal.finish('stopped' if isinstance(exc,KeyboardInterrupt) else 'failed',str(exc))
        raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user-id',type=int)
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--preset',choices=('v3','pe1-v1'),default='v3')
    parser.add_argument('--not-before',type=date.fromisoformat,default=date(2026,10,23))
    parser.add_argument('--poll-seconds',type=int,default=3)
    parser.add_argument('--check',action='store_true',help='Read-only readiness check; never place orders')
    args=parser.parse_args()
    validate_execution_mode(args.preset,args.live)
    if args.poll_seconds<1:parser.error('Poll interval must be positive')
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    now=datetime.now(IST)
    if not args.check and not startup_allowed(now,args.not_before):
        log.info('No start: scheduled eligibility begins %s, startup window 09:15–09:45 IST',args.not_before)
        return
    from nifty_15m_env import load_nifty_environment
    load_nifty_environment()
    from trading.database import get_db
    from trading.user_token import fetch_user_token
    from trading.service_runtime import ServiceRuntimeMonitor
    from market.trading_calendar import is_nse_trading_day
    from trading.reference_live_records import verify_schema,KEY
    with get_db() as db:
        verify_schema(db)
        eligible=is_nse_trading_day(args.not_before if args.check else now.date(),db)
    if args.check:
        kite,user_id=fetch_user_token(log,user_id=args.user_id)
        kite.profile()
        options=[i for i in kite.instruments('NFO') if i.get('name')=='NIFTY' and i.get('instrument_type') in {'CE','PE'} and i.get('expiry') and i['expiry']>=args.not_before]
        if not options:raise RuntimeError('No Nifty options available for scheduled start')
        from trading.reference_live_execution import select_offset_contract
        quote=kite.quote(['NSE:NIFTY 50']).get('NSE:NIFTY 50',{})
        spot=Decimal(str(quote.get('last_price') or 0))
        if not isinstance(quote.get('timestamp'),datetime) or not spot.is_finite() or spot<=0:
            raise RuntimeError('Index quote lacks required price/timestamp fields')
        positions,orders=kite.positions().get('net',[]),kite.orders()
        previews={side:select_offset_contract(options,args.not_before,side,spot,positions,orders)['strike'] for side in ('CE','PE')}
        print(f'Read-only strike preview: CE={previews["CE"]} PE={previews["PE"]}; actual entry reselects using fresh spot and conflicts')
        print(f'CHECK OK: strategy={KEY} user_id={user_id} start={args.not_before} trading_day={eligible} nearest_expiry={min(i["expiry"] for i in options)} orders=DISABLED preset={args.preset} stop=25 max_entries={configured_engine(args.preset).max_entries} lot_count=1')
        return
    if not eligible:
        log.info('NSE holiday/weekend; no session');return
    lock=open('/tmp/nifty_30m_reference_live.lock','a')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:raise RuntimeError('Another V3 runner is active')
    monitor=ServiceRuntimeMonitor('nifty_30m_reference_v3',log,token_alerts_during_market_session=True)
    monitor.start('Checking Kite session for frozen V3')
    try:
        while startup_allowed(datetime.now(IST),args.not_before):
            try:
                kite,user_id=fetch_user_token(log,user_id=args.user_id);kite.profile();break
            except (Exception,SystemExit) as exc:
                if isinstance(exc,SystemExit) and exc.code not in (None,1):raise
                monitor.degraded('Kite session unavailable; retry in 5 minutes before 09:45 cutoff')
                log.warning('Kite session unavailable (%s); retrying',type(exc).__name__)
                time.sleep(300)
        else:
            monitor.stop('Session not authenticated before opening range completed; no late start');return
        monitor.user_id=user_id
        from ema_strategy_runner import process_lock
        from trading.database import engine as db_engine
        with process_lock(db_engine,user_id,KEY,'execution'):
            run_session(kite,user_id,live=args.live,poll_seconds=args.poll_seconds,monitor=monitor,preset=args.preset)
        monitor.stop('Frozen V3 session complete')
    except KeyboardInterrupt:
        monitor.stop('V3 interrupted; reconcile any open position');raise
    except Exception as exc:
        monitor.exception(exc,'Frozen V3 live runner');raise
    finally:
        lock.close()


if __name__=='__main__':
    try:
        main()
    except Exception as exc:
        # Avoid emitting decrypted SQL parameters or credentials in an uncaught traceback.
        log.error('Runner stopped: %s; inspect journal/runtime status for reconciliation',type(exc).__name__)
        raise SystemExit(1)
