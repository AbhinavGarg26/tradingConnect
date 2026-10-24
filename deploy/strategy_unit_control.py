#!/usr/bin/python3 -I
"""Root-installed, stdlib-only allowlisted systemd helper. No database access."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

PROFILES={
 'nifty_15m_breakout':('nifty-15m-v3','nifty_15m_live.py',['--token-attempts','0','--token-retry-seconds','300']),
 'nifty_30m_reference':('nifty-30m-pe1','nifty_30m_reference_live.py',['--preset','pe1-v1']),
 'fixed_profit_filtered':('fixed-profit-filtered','fixed_profit_filtered_live.py',[]),
 'nifty_daily_ema_monthly':('nifty-daily-ema-calls','ema_strategy_runner.py',['--strategy','daily_calls']),
 'nifty_bearish_15m_9_21':('nifty-bearish-15m-ema','ema_strategy_runner.py',['--strategy','bearish_puts']),
 'nifty_bullish_15m_9_21':('nifty-bullish-15m-ema','ema_strategy_runner.py',['--strategy','bullish_calls']),
}
CONFIG=Path('/etc/trading-strategy-control.json')


def systemctl(*args):
    result=subprocess.run(['/usr/bin/systemctl',*args],capture_output=True,text=True,timeout=25,
        env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C','LC_ALL':'C'})
    if result.returncode and not (args[0]=='show' and result.stdout.strip()=='not-found'):
        raise RuntimeError(f'systemctl {args[0]} failed; inspect server journal')
    return result.stdout.strip()


def load_config():
    stat=CONFIG.stat()
    if CONFIG.is_symlink() or stat.st_uid!=0 or stat.st_mode & 0o022:
        raise RuntimeError('Control configuration must be root-owned and not writable by others')
    config=json.loads(CONFIG.read_text())
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+',config['project_dir']):raise ValueError('Invalid project directory')
    if not re.fullmatch(r'[a-z_][a-z0-9_-]*',config['service_user']) or config['service_user']=='root':
        raise ValueError('Services must use an approved unprivileged account')
    return config


def command_for(key,mode,user_id,project):
    _,script,flags=PROFILES[key]
    command=[project+'/.venv/bin/python',project+'/'+script,'--user-id',str(user_id),*flags]
    if script=='ema_strategy_runner.py':
        command+=['--mode',mode,'--skip-incomplete-days']
        if mode=='live':command+=['--acknowledge-premium-risk']
    elif mode=='live':command+=['--live']
    return ' '.join(command)


def write_override(unit,command):
    directory=Path('/etc/systemd/system',unit+'.service.d')
    directory.mkdir(mode=0o755,exist_ok=True)
    descriptor,name=tempfile.mkstemp(prefix='.dashboard-',dir=directory)
    try:
        with os.fdopen(descriptor,'w') as handle:
            handle.write('[Service]\nExecStart=\nExecStart='+command+'\n')
            handle.flush();os.fsync(handle.fileno())
        os.chmod(name,0o644)
        os.replace(name,directory/'90-dashboard.conf')
    finally:
        if os.path.exists(name):os.unlink(name)


def apply(config,user_id,key,operation,mode,ctl=systemctl,write=write_override):
    if int(config['user_id'])!=user_id:raise ValueError('Account is not authorized on this trading server')
    if key not in PROFILES or operation not in {'schedule','start','pause'} or mode not in {'paper','live'}:
        raise ValueError('Unsupported control request')
    unit=PROFILES[key][0]
    if ctl('show',unit+'.timer','--property=LoadState','--value')!='loaded':
        raise RuntimeError('Install the reviewed strategy timer first')
    if operation=='pause':
        ctl('disable','--now',unit+'.timer')
        return 'Future starts paused. Current runner and broker positions were not stopped.'
    if ctl('show',unit+'.service','--property=LoadState','--value')!='loaded':
        raise RuntimeError('Install the reviewed strategy service first')
    if ctl('show',unit+'.service','--property=User','--value')!=config['service_user']:
        raise RuntimeError('Service must run as the approved unprivileged account')
    # Quiesce the timer before checking runner state so it cannot launch a
    # scheduled start between the state check and the override write.
    ctl('disable','--now',unit+'.timer')
    state=ctl('show',unit+'.service','--property=ActiveState','--value')
    if state not in {'inactive','failed'}:raise RuntimeError('Runner is active or changing state; mode change refused')
    job=ctl('show',unit+'.service','--property=Job','--value')
    if job not in {'','0'}:raise RuntimeError('A service job is pending; wait and inspect its result')
    if key=='nifty_30m_reference':
        for suffix in ('service','timer'):
            if ctl('show','nifty-30m-reference-v3.'+suffix,'--property=LoadState','--value')=='not-found':continue
            legacy=ctl('show','nifty-30m-reference-v3.'+suffix,'--property=ActiveState','--value')
            if legacy not in {'inactive','failed',''}:
                raise RuntimeError('Pause the legacy V3 timer/runner before controlling PE1')
    if key in {'nifty_30m_reference','nifty_15m_breakout'}:
        if mode=='live' and ctl('show','strategy-guardian.timer','--property=LoadState','--value')!='loaded':
            raise RuntimeError('Install the strategy guardian timer before scheduling protected live strategies')
    # Disable scheduling before changing the effective command. A partial failure
    # is reported for reconciliation; the worker never blindly repeats it.
    command=command_for(key,mode,user_id,config['project_dir'])
    write(unit,command)
    ctl('daemon-reload')
    effective=ctl('show',unit+'.service','--property=ExecStart','--value')
    arguments=re.findall(r'argv\[\]=(.*?)\s+;',effective)
    if len(arguments)!=1 or arguments[0].split()!=command.split():
        raise RuntimeError('Another service override changed the command; timer remains paused. Review systemctl cat')
    if key in {'nifty_30m_reference','nifty_15m_breakout'} and mode=='live':ctl('enable','--now','strategy-guardian.timer')
    if operation=='schedule':
        ctl('enable','--now',unit+'.timer')
        return f'{mode.upper()} mode configured; future sessions scheduled. Check observed status.'
    ctl('start','--no-block',unit+'.service')
    return f'{mode.upper()} start dispatched; recurring timer remains paused. Check heartbeat and run result.'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('user_id',type=int)
    parser.add_argument('strategy_key',choices=PROFILES)
    parser.add_argument('operation',choices=('schedule','start','pause'))
    parser.add_argument('mode',choices=('paper','live'))
    args=parser.parse_args()
    if os.geteuid()!=0:raise RuntimeError('This helper must be installed and invoked through the restricted sudo rule')
    print(apply(load_config(),args.user_id,args.strategy_key,args.operation,args.mode))

if __name__=='__main__':main()
