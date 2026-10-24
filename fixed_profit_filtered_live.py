"""Frozen Fixed Profit candidate: paper by default, user-operated --live mode.

This process must run for index-based stop/target exits. A quote trigger is not
an exchange-native protective order and does not guarantee the configured fill.
"""
import argparse
from datetime import date, datetime, time as clock_time, timedelta
from decimal import Decimal
import logging
from pathlib import Path
import time

from strategies.fixed_profit_filtered import FilteredSession, CONFIG, CONFIG_HASH
from strategies.nifty_15m_breakout import Bar
from strategies.nifty_opening_range import IST
from nifty_15m_env import load_nifty_environment
load_nifty_environment()

from trading.fixed_profit_execution import FixedProfitExecutor, TAG
from trading.fixed_profit_journal import SessionJournal
from trading.reference_live_execution import fresh_price, FINAL

log=logging.getLogger(__name__)


def completed_minutes(records,now):
    bars=[]
    for row in records:
        stamp=row['date']
        if stamp.tzinfo is None:stamp=stamp.replace(tzinfo=IST)
        stamp=stamp.astimezone(IST)
        if stamp.date()!=now.astimezone(IST).date():raise ValueError('Wrong historical session')
        if stamp+timedelta(minutes=1,seconds=2)<=now:
            bars.append(Bar(stamp,*(Decimal(str(row[k])) for k in ('open','high','low','close'))))
    return bars


def startup_allowed(now):
    return now.weekday()<5 and clock_time(9,15)<=now.time()<clock_time(9,45)


def run_session(kite,user_id,*,live=False,state_dir='outputs/fixed-profit-runtime',poll_seconds=3,
                now_fn=None,sleep_fn=time.sleep,journal_factory=SessionJournal):
    now_fn=now_fn or (lambda:datetime.now(IST))
    today=now_fn().date()
    journal=journal_factory(state_dir,user_id,today,live=live)
    try:
        if any(o.get('tag')==TAG for o in kite.orders()):
            raise RuntimeError('Fixed-profit broker order already exists today; reconcile before restart')
        instruments=kite.instruments('NFO')
        nifty=next((i for i in kite.instruments('NSE') if i.get('tradingsymbol')=='NIFTY 50'),None)
        if not nifty:raise RuntimeError('Missing NIFTY index instrument')
        session=FilteredSession()
        executor=FixedProfitExecutor(kite,instruments,journal,live=live,now_fn=now_fn)
        last=None
        while True:
            now=now_fn()
            if now.date()!=today:raise RuntimeError('Session changed; reconcile before restart')
            # Exit checks precede historical requests, so delayed candles cannot
            # suppress a stop/target or the 15:15 time exit when quotes work.
            if executor.position:
                spot=fresh_price(kite,'NSE:NIFTY 50',now)
                signal=session.engine.on_tick(now,spot)
                if signal:executor.exit(signal,session.engine)
            if executor.position is None and (session.skipped or session.engine.entries>=1 or now.time()>=clock_time(14,30)):
                journal.finish('completed');return
            records=kite.historical_data(nifty['instrument_token'],f'{today} 09:15:00',
                                         now.strftime('%Y-%m-%d %H:%M:%S'),'minute')
            for bar in completed_minutes(records,now):
                if last and bar.time<=last:continue
                session.on_minute(bar,observed_at=now);last=bar.time
                if len(session.minutes)==30 and not session.engine.eligible:
                    journal.event('SESSION_SKIPPED',reason='opening_range_not_above_40')
                    journal.finish('completed');return
                if session.engine.pending and session.engine.pending.action.startswith('BUY_'):
                    executor.enter(session)
                    if session.skipped:
                        journal.event('SESSION_SKIPPED',reason='original_entry_rejected_by_filter_or_stale')
                        journal.finish('completed');return
            journal.heartbeat()
            sleep_fn(poll_seconds)
    except BaseException as exc:
        journal.event('RECONCILE_REQUIRED',error=type(exc).__name__)
        journal.finish('stopped' if isinstance(exc,KeyboardInterrupt) else 'failed')
        raise
    finally:journal.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user-id',type=int,required=True)
    parser.add_argument('--live',action='store_true',help='User-operated real broker orders; default is paper')
    parser.add_argument('--check',action='store_true',help='Read-only broker/calendar readiness; never starts a session')
    parser.add_argument('--state-dir',default='outputs/fixed-profit-runtime')
    args=parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    now=datetime.now(IST)
    if not args.check and not startup_allowed(now):
        print('No session started: start on a trading day between 09:15 and 09:45 IST.');return
    from nifty_15m_env import load_nifty_environment
    load_nifty_environment()
    from trading.user_token import fetch_user_token
    from trading.database import get_db
    from market.trading_calendar import is_nse_trading_day
    from sqlalchemy import inspect
    from trading.database import engine as db_engine
    schema=inspect(db_engine)
    if any(not schema.has_table(t) for t in ('strategy_runs','strategy_trades','strategy_events')):
        raise RuntimeError('Apply strategy journal migrations before running')
    columns={c['name']:c for c in schema.get_columns('strategy_trades')}
    if any(not columns[k]['nullable'] for k in ('initial_stop','current_stop','target')):
        raise RuntimeError('Apply nullable stop/target migrations before running')
    with get_db() as db:eligible=is_nse_trading_day(now.date(),db)
    kite,user_id=fetch_user_token(log,user_id=args.user_id);kite.profile()
    if args.check:
        instruments=kite.instruments('NFO')
        available=[i for i in instruments if i.get('name')=='NIFTY' and i.get('instrument_type') in {'CE','PE'} and i.get('expiry') and i['expiry']>=now.date()]
        if not available:raise RuntimeError('No current NIFTY option instruments')
        if any(o.get('tag')==TAG for o in kite.orders()):raise RuntimeError('Unresolved strategy broker order')
        print(f'Broker authenticated; calendar_trading_day={eligible}; orders=DISABLED; config_hash={CONFIG_HASH}')
        print(dict(CONFIG));return
    if not eligible:print('No session: exchange calendar is closed.');return
    from trading.fixed_profit_db_journal import DatabaseSessionJournal
    run_session(kite,user_id,live=args.live,state_dir=args.state_dir,journal_factory=DatabaseSessionJournal)


if __name__=='__main__':
    try:main()
    except Exception as exc:
        log.error('Fixed-profit stopped (%s); inspect durable journal and reconcile before restart.',type(exc).__name__)
        raise SystemExit(1)
