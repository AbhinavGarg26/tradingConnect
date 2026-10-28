"""Read-only systemd inventory; only its observed status is written to the DB."""
import argparse
from datetime import datetime, timezone
import json
import re
import socket
import subprocess
from pathlib import Path

UNITS={
    'nifty_15m_breakout':('nifty-15m-v3','nifty_15m_live.py',''),
    'nifty_30m_reference':('nifty-30m-pe1','nifty_30m_reference_live.py','--preset pe1-v1'),
    'fixed_profit_filtered':('fixed-profit-filtered','fixed_profit_filtered_live.py',''),
    'nifty_daily_ema_monthly':('nifty-daily-ema-calls','ema_strategy_runner.py','--strategy daily_calls'),
    'nifty_bearish_15m_9_21':('nifty-bearish-15m-ema','ema_strategy_runner.py','--strategy bearish_puts'),
    'nifty_bullish_15m_9_21':('nifty-bullish-15m-ema','ema_strategy_runner.py','--strategy bullish_calls'),
}

RUNTIME_SERVICES={
    'nifty_15m_v3':dict(unit='nifty-15m-v3',timer=True,category='strategy',display_name='Nifty 15m v3'),
    'nifty_30m_reference_v3':dict(unit='nifty-30m-pe1',timer=True,category='strategy',display_name='Nifty 30m PE1'),
    'fixed_profit_filtered':dict(unit='fixed-profit-filtered',timer=True,category='strategy',display_name='Fixed profit filtered'),
    'nifty_daily_ema_monthly':dict(unit='nifty-daily-ema-calls',timer=True,category='strategy',display_name='Nifty daily EMA calls'),
    'nifty_bearish_15m_9_21':dict(unit='nifty-bearish-15m-ema',timer=True,category='strategy',display_name='Nifty bearish 15m EMA'),
    'nifty_bullish_15m_9_21':dict(unit='nifty-bullish-15m-ema',timer=True,category='strategy',display_name='Nifty bullish 15m EMA'),
    'strategy_guardian':dict(unit='strategy-guardian',timer=True,category='support',display_name='Strategy guardian'),
    'strategy_control_worker':dict(unit='strategy-control-worker',timer=False,category='support',display_name='Strategy control worker'),
    'strategy_status_observer':dict(unit='strategy-status-observer',timer=True,category='support',display_name='Strategy status observer',prefer_timer_state=True,schedule_label='Every 60 seconds'),
    'kite_market_fetcher':dict(unit='kite-market-fetcher',timer=False,category='support',display_name='Kite market fetcher'),
    'market_breath':dict(unit='market-breath',timer=True,category='support',display_name='Market breath'),
}


def inspect_unit(unit):
    try:
        result=subprocess.run(['systemctl','show',unit,'--no-pager',
            '--property=LoadState,ActiveState,SubState,UnitFileState,NextElapseUSecRealtime,ExecStart,Result,EnvironmentFiles,MainPID'],
            capture_output=True,text=True,timeout=10,check=False)
        fields=dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
        return fields if fields.get('LoadState') else {'LoadState':'unknown'}
    except (OSError,subprocess.SubprocessError):return {'LoadState':'unknown'}


def configured_account(command,service):
    account=re.search(r'--user-id(?:=|\s+)(\d+)(?=\s|;|$)',command)
    if account:return int(account.group(1))
    if not re.search(r'--user-id\s+\$\{EMA_USER_ID\}(?=\s|;|$)',command):return None
    # systemctl show can retain the variable until process execution. Resolve
    # only the known ID from one environment file; never persist file contents.
    source=re.fullmatch(r'(\S+) \(ignore_errors=(?:yes|no)\)',service.get('EnvironmentFiles',''))
    if not source:return None
    try:
        values=re.findall(r'^\s*EMA_USER_ID=[\x22\x27]?(\d+)[\x22\x27]?\s*$',Path(source.group(1)).read_text(),re.MULTILINE)
        return int(values[-1]) if values else None
    except (OSError,ValueError):return None


def observe(key,probe=inspect_unit,expected_user_id=None):
    unit,script,required=UNITS[key]
    service,timer=probe(unit+'.service'),probe(unit+'.timer')
    command=service.get('ExecStart','')
    compatible=script in command and (not required or required in command)
    issue='' if compatible else 'Service command does not match this strategy.'
    if expected_user_id is not None:
        bound=configured_account(command,service)==expected_user_id
        if compatible and not bound:issue='Service account binding is unverified; last run mode is shown separately.'
        compatible=compatible and bound
    mode='unknown'
    if compatible:
        # Unknown if a command delegates mode selection to an unresolved variable.
        if '--mode live' in command or ' --live' in command:mode='live'
        elif '--mode paper' in command or ('--mode' not in command and '$' not in command.replace('${EMA_USER_ID}','')):mode='paper'
    return dict(service_unit=unit+'.service',timer_unit=unit+'.timer',
        service_state=service.get('ActiveState','unknown'),service_loaded=service.get('LoadState','unknown'),
        service_substate=service.get('SubState','unknown'),main_pid=service.get('MainPID','0'),
        timer_state=timer.get('ActiveState','unknown'),timer_loaded=timer.get('LoadState','unknown'),
        timer_enabled=timer.get('UnitFileState','unknown'),next_run=timer.get('NextElapseUSecRealtime',''),
        configured_mode=mode,command_matches=compatible,command_issue=issue,result=service.get('Result','unknown'))


def runtime_record(service_name, definition, probe=inspect_unit, strategy_details=None):
    unit=definition['unit']
    service=probe(unit+'.service')
    timer=probe(unit+'.timer') if definition['timer'] else {}
    service_state=service.get('ActiveState','unknown')
    timer_state=timer.get('ActiveState','unknown')
    timer_enabled=timer.get('UnitFileState','unknown')
    next_run=timer.get('NextElapseUSecRealtime','')
    if definition.get('prefer_timer_state') and timer_state=='active' and timer_enabled=='enabled':
        status='scheduled'
        activity='Healthy; runs every 60 seconds'
        pid=0
    elif service_state in {'active','activating','reloading'}:
        status='active'
        activity='Running'
    elif service_state=='failed' or service.get('Result')=='failed':
        status='error'
        activity='Service failed; inspect journal'
    elif timer_state=='active' and timer_enabled=='enabled':
        status='scheduled'
        activity='Waiting for scheduled start'
    else:
        status='stopped'
        activity='Stopped; timer disabled' if definition['timer'] else 'Stopped'
    details=strategy_details or {}
    metadata=dict(
        managed_service=True,category=definition['category'],display_name=definition['display_name'],
        systemd_service=unit+'.service',systemd_timer=unit+'.timer' if definition['timer'] else None,
        service_state=service_state,service_substate=service.get('SubState','unknown'),
        timer_state=timer_state,timer_enabled=timer_enabled,next_run=next_run,
        schedule_label=definition.get('schedule_label',''),
        configured_mode=details.get('configured_mode',''),command_matches=details.get('command_matches',True),
    )
    if not (definition.get('prefer_timer_state') and timer_state=='active' and timer_enabled=='enabled'):
        try:pid=int(service.get('MainPID','0') or 0)
        except (TypeError,ValueError):pid=0
    return dict(service_name=service_name,status=status,pid=pid or None,activity=activity,metadata=metadata)


def publish_runtime_statuses(db,user_id,records):
    from sqlalchemy import text
    query=text('''INSERT INTO service_runtime_statuses
        (user_id,service_name,status,pid,host_name,current_activity,last_heartbeat_at,
         token_status,metadata,created_at,updated_at)
        VALUES (:user_id,:service_name,:status,:pid,:host_name,:activity,:now,
                'unknown',CAST(:metadata AS jsonb),:now,:now)
        ON CONFLICT (user_id,service_name) DO UPDATE SET
          status=EXCLUDED.status,pid=EXCLUDED.pid,host_name=EXCLUDED.host_name,
          current_activity=EXCLUDED.current_activity,last_heartbeat_at=EXCLUDED.last_heartbeat_at,
          metadata=EXCLUDED.metadata,updated_at=EXCLUDED.updated_at''')
    now=datetime.now(timezone.utc)
    for record in records:
        db.execute(query,dict(user_id=user_id,host_name=socket.gethostname(),now=now,
            metadata=json.dumps(record['metadata']),**{k:record[k] for k in ('service_name','status','pid','activity')}))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user-id',type=int,required=True)
    parser.add_argument('--print-only',action='store_true')
    args=parser.parse_args()
    records={key:observe(key,expected_user_id=args.user_id) for key in UNITS}
    if args.print_only:print(json.dumps(records,indent=2));return
    from nifty_15m_env import load_nifty_environment
    load_nifty_environment()
    from trading.database import get_db
    from sqlalchemy import text
    with get_db() as db:
        for key,details in records.items():
            db.execute(text('''INSERT INTO strategy_service_observations
                (user_id,strategy_key,observed_at,details,created_at,updated_at)
                VALUES (:user_id,:key,:now,CAST(:details AS jsonb),:now,:now)
                ON CONFLICT (user_id,strategy_key) DO UPDATE SET
                observed_at=EXCLUDED.observed_at,details=EXCLUDED.details,updated_at=EXCLUDED.updated_at'''),
                dict(user_id=args.user_id,key=key,now=datetime.now(timezone.utc),details=json.dumps(details)))
        strategy_by_service={
            'nifty_15m_v3':records['nifty_15m_breakout'],
            'nifty_30m_reference_v3':records['nifty_30m_reference'],
            'fixed_profit_filtered':records['fixed_profit_filtered'],
            'nifty_daily_ema_monthly':records['nifty_daily_ema_monthly'],
            'nifty_bearish_15m_9_21':records['nifty_bearish_15m_9_21'],
            'nifty_bullish_15m_9_21':records['nifty_bullish_15m_9_21'],
        }
        publish_runtime_statuses(db,args.user_id,[runtime_record(name,definition,
            strategy_details=strategy_by_service.get(name)) for name,definition in RUNTIME_SERVICES.items()])

    # Alerts use committed events and cannot affect execution or observer status.
    try:
        from trading.strategy_signal_alerts import dispatch
        dispatch(get_db,args.user_id,datetime.now(timezone.utc))
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning('Strategy alert dispatch failed (%s)',type(exc).__name__)

if __name__=='__main__':main()
