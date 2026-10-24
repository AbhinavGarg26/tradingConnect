"""Fresh replay of baseline 15-stop / 30-target entries with closed 30m EMA gates.

All filters retain original eligible entry times or skip the session; no later
replacement entries. Reports identical common dates and an explicit EMA warmup.
"""
import argparse
import csv
import json
from bisect import bisect_right
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from backtest.fixed_profit import load_sessions, run_session, statistics
from backtest.fixed_profit_ema import ClosedEMA
from backtest.fixed_profit_diagnostics import periods
from strategies.fixed_profit_trend_gate import TrendSnapshot, allows_entry

CONFIG=dict(setup_mode='retest',confirmation_minutes=5,min_range_points=40,
            tolerance_points=2,follow_through=False,stop_points=15,entry_stop_points=10)
GATES={'baseline':None,'ema36':((36,),True),'ema100':((100,),True),
       'dual_price_only':((36,100),False),'dual_price_and_close':((36,100),True)}


def schedule(sessions, *, include_partial=True):
    ema36,ema100=ClosedEMA(36),ClosedEMA(100)
    snapshots={};opening_values={}
    for day,bars in sessions:
        closes=list(range(29,360,30))+([374] if include_partial else [])
        for i in closes:
            end=bars[i].time+timedelta(minutes=1)
            for ema in (ema36,ema100):ema.update(bars[i].close,end)
            if ema100.count>=300:
                snapshot=TrendSnapshot(end,bars[i].close,ema36.value,ema100.value)
                snapshots[end]=snapshot
                if i==29:opening_values[day]=snapshot
    return snapshots,opening_values


def export_trades(path,rows):
    with path.open('w') as f:
        if rows:
            writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)


def score(rows):
    return dict(metrics=periods(rows),yearly={str(y):statistics([r for r in rows if r['date'].startswith(str(y))]) for y in range(2020,2027)},
                by_side={s:statistics([r for r in rows if r['side']==s]) for s in ('CE','PE')})


def analyze(path,output):
    sessions,excluded=load_sessions(path)
    snapshots,openings=schedule(sessions)
    full_only,full_openings=schedule(sessions,include_partial=False)
    stamps=sorted(snapshots);full_stamps=sorted(full_only)
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    all_baseline=[];shared=[];excluded_warmup=[];case=None
    for day,bars in sessions:
        trade=run_session(bars,CONFIG)
        if not trade:continue
        all_baseline.append(trade)
        if day not in openings:
            excluded_warmup.append(trade['date']);continue
        entry_time=datetime.fromisoformat(trade['entry_time'])
        snapshot=snapshots[stamps[bisect_right(stamps,entry_time)-1]]
        row={**trade,'ema_closed_at':snapshot.closed_at.isoformat(),
             'last_30m_close':float(snapshot.close),'ema36':float(snapshot.ema36),'ema100':float(snapshot.ema100)}
        for name,settings in GATES.items():
            row[name+'_allowed']=(True if settings is None else allows_entry(trade['entry'],trade['side'],snapshot,
                                                   observed_at=entry_time,periods=settings[0],require_close=settings[1]))
        shared.append(row)
        if trade['date']=='2026-09-21':
            full_snapshot=full_only[full_stamps[bisect_right(full_stamps,entry_time)-1]]
            case=dict(trade=row,opening=dict(ema36=float(openings[day].ema36),ema100=float(openings[day].ema100)),
                      screenshot_opening=dict(ema36=23313.72,ema100=23420.41),
                      full_only_entry=dict(ema36=float(full_snapshot.ema36),ema100=float(full_snapshot.ema100),close=float(full_snapshot.close)),
                      full_only_opening=dict(ema36=float(full_openings[day].ema36),ema100=float(full_openings[day].ema100)))
    comparisons={}
    for name in GATES:
        rows=[r for r in shared if r[name+'_allowed']]
        comparisons[name]=score(rows)
        export_trades(output/f'{name}_trades.csv',rows)
        print(name,comparisons[name]['metrics']['all'],flush=True)
    # Previous saved 307-entry identifiers must be reproduced by this replay.
    original_path=Path('outputs/fixed-profit-v2/retest_trades.csv')
    original_match=None
    if original_path.exists():
        with original_path.open() as f:original=list(csv.DictReader(f))
        identity=lambda rows:[(r['date'],r['side'],r['entry_time'],float(r['entry'])) for r in rows]
        original_match=identity(original)==identity(all_baseline)
        if not original_match:raise ValueError('Fresh baseline entries differ from archived original entries')
    # Sensitivity of the specified gate to full-only 30m bars, on same shared dates.
    full_sensitivity=[];full_common=[]
    for row in shared:
        day=datetime.fromisoformat(row['entry_time']).date()
        if day not in full_openings:continue
        full_common.append(row)
        stamp=datetime.fromisoformat(row['entry_time'])
        snapshot=full_only[full_stamps[bisect_right(full_stamps,stamp)-1]]
        if allows_entry(row['entry'],row['side'],snapshot,observed_at=stamp):full_sensitivity.append(row)
    report=dict(source=str(path),sessions=len(sessions),excluded_incomplete_dates=excluded,
                start=str(sessions[0][0]),end=str(sessions[-1][0]),
                ema_eligible_sessions=len(openings),baseline_before_warmup=score(all_baseline),
                baseline_entries_match_archived=original_match,baseline_trade_dates_excluded_for_warmup=excluded_warmup,
                ema_method='SMA seed, recursive EMA; current-session 30m close events including final 15m partial bar; 300-event warmup; carry across sessions',
                comparisons=comparisons,september21=case,
                full_bars_only_sensitivity=score(full_sensitivity),
                full_bars_only_common_baseline=score(full_common))
    (output/'analysis.json').write_text(json.dumps(report,indent=2))
    print('Sep21:',json.dumps(case,indent=2),flush=True)
    print('Warmup excluded trades:',excluded_warmup,flush=True)
    print('Original identities match:',original_match,flush=True)
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv_path');parser.add_argument('--output',default='outputs/fixed-profit-36-100')
    args=parser.parse_args();analyze(args.csv_path,args.output)
