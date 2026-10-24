"""Read-only systemd inventory; only its observed status is written to the DB."""
import argparse
from datetime import datetime, timezone
import json
import re
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


def inspect_unit(unit):
    try:
        result=subprocess.run(['systemctl','show',unit,'--no-pager',
            '--property=LoadState,ActiveState,UnitFileState,NextElapseUSecRealtime,ExecStart,Result,EnvironmentFiles'],
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
        timer_state=timer.get('ActiveState','unknown'),timer_loaded=timer.get('LoadState','unknown'),
        timer_enabled=timer.get('UnitFileState','unknown'),next_run=timer.get('NextElapseUSecRealtime',''),
        configured_mode=mode,command_matches=compatible,command_issue=issue,result=service.get('Result','unknown'))


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

    # Alerts use committed events and cannot affect execution or observer status.
    try:
        from trading.strategy_signal_alerts import dispatch
        dispatch(get_db,args.user_id,datetime.now(timezone.utc))
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning('Strategy alert dispatch failed (%s)',type(exc).__name__)

if __name__=='__main__':main()
