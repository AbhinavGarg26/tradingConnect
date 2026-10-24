"""Consume authenticated page requests. Never run from a web request or as root."""
import argparse
from datetime import datetime, timedelta, timezone, time
import logging
import subprocess
import time as clock
from zoneinfo import ZoneInfo
from sqlalchemy import text

log=logging.getLogger(__name__)
KEYS={'nifty_15m_breakout','nifty_30m_reference','fixed_profit_filtered','nifty_daily_ema_monthly','nifty_bearish_15m_9_21'}
IST=ZoneInfo('Asia/Kolkata')


def utcstamp(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def validate_request(row,db,now):
    if row['strategy_key'] not in KEYS or row['operation'] not in {'start','schedule','pause'} or row['mode'] not in {'paper','live'}:
        raise ValueError('Unsupported request')
    if row['operation']=='pause':return
    if now-utcstamp(row['created_at'])>timedelta(minutes=5):raise ValueError('Request expired; review current state and submit again')
    if row['mode']=='live' and not row['risk_acknowledged']:raise ValueError('Live risk acknowledgement missing')
    unresolved=db.execute(text('''SELECT id FROM strategy_trades WHERE user_id=:user_id
        AND strategy_key=:key AND status != 'closed' LIMIT 1'''),dict(user_id=row['user_id'],key=row['strategy_key'])).first()
    if unresolved:raise ValueError('Open or unresolved strategy position; reconcile before activation/mode changes')
    if row['strategy_key'] in {'nifty_daily_ema_monthly','nifty_bearish_15m_9_21'}:
        if db.execute(text('''SELECT id FROM strategy_order_intents WHERE user_id=:user_id
            AND strategy_key=:key AND (status IN ('reserved','submitting','submitted','uncertain')
                OR (status='filled' AND applied_at IS NULL)) LIMIT 1'''),
            dict(user_id=row['user_id'],key=row['strategy_key'])).first():
            raise ValueError('Unresolved EMA order reservation; reconcile first')
        if db.execute(text('''SELECT id FROM strategy_execution_states WHERE user_id=:user_id
            AND strategy_key=:key AND (status IN ('halted','reconcile') OR pending_signal != '{}'::jsonb) LIMIT 1'''),
            dict(user_id=row['user_id'],key=row['strategy_key'])).first():
            raise ValueError('EMA checkpoint requires reconciliation before activation')
    if row['mode']=='live' and row['strategy_key'] in {'nifty_30m_reference','nifty_15m_breakout'}:
        risk=db.execute(text('''SELECT * FROM strategy_risk_settings WHERE user_id=:user_id
            AND strategy_key=:key'''),dict(user_id=row['user_id'],key=row['strategy_key'])).mappings().first()
        if not risk or not risk['live_enabled'] or risk['stop_mode']!='broker_stop_limit' or risk['adverse_action']!='emergency_exit':
            raise ValueError('Save live-approved protected risk settings first')
        if not 0<risk['risk_per_trade']<=risk['amount_allocated'] or not 0<risk['max_daily_loss']<=risk['amount_allocated']:
            raise ValueError('Protected strategy allocation/loss limits are invalid')
        if row['operation']=='start':
            guardian=db.execute(text('''SELECT heartbeat_at,status FROM strategy_guardian_statuses
                WHERE user_id=:user_id AND strategy_key=:key'''),dict(user_id=row['user_id'],key=row['strategy_key'])).mappings().first()
            if not guardian or not guardian['heartbeat_at'] or now-utcstamp(guardian['heartbeat_at'])>timedelta(seconds=90) or guardian['status']!='healthy':
                raise ValueError('Independent guardian must be healthy before starting a protected live strategy')
    if row['operation']=='start':
        local=now.astimezone(IST)
        opening=time(9,15);cutoff=time(15,30) if row['strategy_key'] in {'nifty_daily_ema_monthly','nifty_bearish_15m_9_21'} else time(9,45)
        if row['strategy_key']=='nifty_15m_breakout':cutoff=time(15,0)
        holiday=db.execute(text('SELECT 1 FROM trading_holidays WHERE holiday_date=:day'),dict(day=local.date())).first()
        if local.weekday()>=5 or holiday or not opening<=local.time()<cutoff:
            raise ValueError('Outside this strategy startup window; use Schedule future sessions')


def helper(row):
    result=subprocess.run(['/usr/bin/sudo','-n','/usr/local/sbin/strategy-unit-control',str(row['user_id']),
        row['strategy_key'],row['operation'],row['mode']],capture_output=True,text=True,timeout=240)
    if result.returncode:
        log.error('Control helper failed: %s',result.stderr[-2000:])
        raise RuntimeError('Service control failed or partially applied. Inspect server logs and observed state before retrying.')
    return result.stdout.strip()[:2000]


def process_one(factory,user_id,invoke=helper,now_fn=lambda:datetime.now(timezone.utc)):
    now=now_fn()
    with factory() as db:
        db.execute(text('''UPDATE strategy_control_requests SET status='uncertain',
            result_message='Worker interrupted; inspect effective service/timer state before resolving this request',
            updated_at=:now WHERE user_id=:user_id AND status='processing' AND updated_at<:cutoff'''),
            dict(now=now,user_id=user_id,cutoff=now-timedelta(minutes=5)))
        row=db.execute(text('''SELECT * FROM strategy_control_requests WHERE user_id=:user_id AND status='queued'
            ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED'''),dict(user_id=user_id)).mappings().first()
        if not row:return False
        row=dict(row)
        try:validate_request(row,db,now)
        except ValueError as exc:
            db.execute(text("UPDATE strategy_control_requests SET status='failed',result_message=:message,processed_at=:now,updated_at=:now WHERE id=:id"),dict(id=row['id'],message=str(exc),now=now))
            return True
        db.execute(text("UPDATE strategy_control_requests SET status='processing',updated_at=:now WHERE id=:id"),dict(id=row['id'],now=now))
    # Claim is durable before invoking the helper. A crash is never auto-retried.
    try:message=invoke(row);status='succeeded'
    except Exception:
        log.exception('Strategy control helper failed for request %s',row['id'])
        message='Outcome uncertain. Inspect service/timer state and worker journal; reconcile before submitting another request.';status='uncertain'
    with factory() as db:
        db.execute(text('UPDATE strategy_control_requests SET status=:status,result_message=:message,processed_at=:now,updated_at=:now WHERE id=:id AND status=\'processing\''),
            dict(id=row['id'],status=status,message=message,now=now_fn()))
    return True


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user-id',type=int,required=True)
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args()
    from nifty_15m_env import load_nifty_environment
    load_nifty_environment()
    from trading.database import get_db
    logging.basicConfig(level=logging.INFO)
    while True:
        process_one(get_db,args.user_id)
        if args.once:return
        clock.sleep(5)

if __name__=='__main__':main()
