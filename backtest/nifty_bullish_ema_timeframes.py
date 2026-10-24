"""Calls-only EMA signal comparison; daily 20/50 baseline, no option P&L claims.

Mirror the PE research's source dates, EMA seed and adverse point slippage.
No broker connection, option orders, runner configuration or journals are edited.
"""
import argparse
import csv
import hashlib
import json
import math
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from backtest.nifty_bearish_ema_timeframes import SOURCES, verify_baseline
from backtest.nifty_daily_ema import validate_daily, save_csv
from backtest.nifty_daily_ema_switch import summarize
from backtest.snapshot_cache import exchange_time, valid_ohlc

ROOT=Path(__file__).resolve().parents[1]
IST=ZoneInfo('Asia/Kolkata')
CASES=((9,21),(10,30),(12,36),(20,50),(20,100),(50,200),(1,12),(1,36))


def prepare(rows,timeframe):
    if timeframe=='1d':
        return [dict(b,date=datetime.combine(b['date'],time(9,15),tzinfo=IST),duration_minutes=375)
                for b in validate_daily(rows)]
    minutes={'15m':15,'30m':30,'1h':60}.get(timeframe)
    if minutes is None:raise ValueError('Choose 15m, 30m, 1h or 1d')
    output=[];previous=None
    for row in rows:
        stamp=exchange_time(row['date']);duration=int(row['duration_minutes'])
        if duration not in (15,minutes) or not valid_ohlc(row):raise ValueError('Invalid session candle')
        if previous is not None and stamp<previous:raise ValueError('Candles must be unique, chronological and non-overlapping')
        previous=stamp+timedelta(minutes=duration)
        output.append(dict(date=stamp,duration_minutes=duration,**{k:float(row[k]) for k in ('open','high','low','close')}))
    return output


def run(rows,fast,slow,*,timeframe='1d',evaluation_start='2021-01-01',slippage_points=.5):
    if type(fast) is not int or type(slow) is not int or not 1<=fast<slow:
        raise ValueError('Require integer periods 1 <= fast < slow')
    if not math.isfinite(slippage_points) or slippage_points<0:raise ValueError('Invalid slippage')
    bars=prepare(rows,timeframe)
    start=date.fromisoformat(evaluation_start)
    first=next((i for i,b in enumerate(bars) if b['date'].date()>=start),len(bars))
    if first<slow or first>=len(bars):raise ValueError('Insufficient common warmup/evaluation history')
    f=s=None;pending=position=None
    trades=[];signals=[];curve=[]
    closed=peak=drawdown=0
    for i,b in enumerate(bars):
        if pending:
            if pending['action']=='enter_call_signal':
                position=dict(side='CE',entry_signal_time=pending['time'],entry_time=b['date'],
                              entry=b['open']+slippage_points,entry_bar=i,
                              signal_fast_ema=pending['fast'],signal_slow_ema=pending['slow'])
            else:
                fill=b['open']-slippage_points;points=fill-position['entry']
                trades.append(dict(position,exit_signal_time=pending['time'],exit_time=b['date'],exit=fill,
                                   points=points,holding_bars=i-position['entry_bar'],
                                   holding_calendar_days=(b['date']-position['entry_time']).total_seconds()/86400))
                closed+=points;position=None
            pending=None
        pf,ps=f,s
        f=b['close'] if f is None else f+2/(fast+1)*(b['close']-f)
        s=b['close'] if s is None else s+2/(slow+1)*(b['close']-s)
        if i<first:continue
        cross_up=pf<=ps and f>s;cross_down=pf>=ps and f<s
        action='enter_call_signal' if position is None and cross_up else 'exit_to_cash' if position and cross_down else None
        if action:
            pending=dict(action=action,time=b['date']+timedelta(minutes=b['duration_minutes']),fast=f,slow=s)
            signals.append(dict(pending,previous_fast=pf,previous_slow=ps,candle_open=b['date']))
        marked=closed+(b['close']-position['entry'] if position else 0)
        peak=max(peak,marked);drawdown=max(drawdown,peak-marked)
        curve.append(dict(time=b['date']+timedelta(minutes=b['duration_minutes']),
                          cumulative_marked_index_points=marked,drawdown_points=peak-marked,
                          position='CE_signal' if position else 'cash',fast_ema=f,slow_ema=s))
    violations=sum(not(sig['previous_fast']<=sig['previous_slow'] and sig['fast']>sig['slow'])
                   for sig in signals if sig['action']=='enter_call_signal')
    exit_violations=sum(not(sig['previous_fast']>=sig['previous_slow'] and sig['fast']<sig['slow'])
                        for sig in signals if sig['action']=='exit_to_cash')
    if violations or exit_violations:raise AssertionError('Bullish crossover entry/exit audit failed')
    stats=summarize([dict(t,holding_sessions=t['holding_bars']) for t in trades])
    stats['average_holding_bars']=stats.pop('average_holding_sessions')
    exposure=100*sum(c['position']=='CE_signal' for c in curve)/len(curve)
    report=dict(timeframe=timeframe,fast=fast,slow=slow,price_cross=fast==1,
                first_time=curve[0]['time'],last_time=curve[-1]['time'],evaluation_bars=len(curve),stats=stats,
                max_marked_drawdown_points=drawdown,total_marked_points=curve[-1]['cumulative_marked_index_points'],
                open_position=position,pending_signal=pending,exposure_pct=exposure,entry_rule_violations=violations,exit_rule_violations=exit_violations,
                slippage_points_per_fill=slippage_points,
                actual_monthly_option_backtest_status='blocked_missing_historical_option_prices',
                limitations='Bullish Nifty signal proxy, not option premiums, monthly rolls, premium-cap results or rupee returns. '
                            'Start flat and wait for fresh upward cross; next-open fills; cash after downward cross; overnight holds; no stop/target. '
                            'Final open leg marked separately and excluded from closed wins. Same first-close EMA seed as baseline/PE research.')
    return report,trades,signals,curve


def period_stats(trades):
    # Attribution by EXIT date; positions are not force-closed at period boundaries.
    return {label:summarize([dict(t,holding_sessions=t['holding_bars']) for t in trades
                           if lo<=t['exit_time'].date().isoformat()<hi])
            for label,lo,hi in [('earlier','2021','2024'),('middle','2024','2026'),('recent','2026','2027')]}


def compare(output,*,evaluation_start='2021-01-01'):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    verify_baseline()  # PE reference inputs/trades are read-only and hash-verified.
    results={};source_manifest={};excluded_reference=None
    for timeframe,path,metadata_path,_ in SOURCES:
        source=ROOT/path
        with source.open() as f:rows=list(csv.DictReader(f))
        metadata=json.loads((ROOT/metadata_path).read_text())
        excluded=metadata['excluded_incomplete_days']
        if excluded_reference is not None and set(excluded)!=set(excluded_reference):
            raise ValueError('Timeframe inputs have different excluded source dates')
        excluded_reference=excluded
        source_manifest[timeframe]=dict(path=path,sha256=hashlib.sha256(source.read_bytes()).hexdigest(),metadata=metadata_path)
        for fast,slow in CASES:
            name=f'{timeframe}_{fast}_{slow}'
            summary,trades,signals,curve=run(rows,fast,slow,timeframe=timeframe,evaluation_start=evaluation_start)
            summary['periods']=period_stats(trades)
            # Extra point deductions are sensitivities in addition to the 0.5/fill adverse slippage.
            summary['extra_cost_sensitivity']={str(cost):summarize([dict(t,points=t['points']-cost,holding_sessions=t['holding_bars']) for t in trades]) for cost in (2,5)}
            results[name]=summary
            folder=output/name;folder.mkdir(exist_ok=True)
            (folder/'summary.json').write_text(json.dumps(summary,indent=2,default=str,allow_nan=False))
            for kind,values in [('trades',trades),('signals',signals),('equity_points',curve)]:save_csv(folder/f'{kind}.csv',values)
            print(name,summary['stats']['closed_trades'],round(summary['stats']['net_index_points'],2),flush=True)
    baseline=results['1d_20_50']
    from backtest.nifty_daily_ema import run as existing_daily, Config
    _,expected,_,_=existing_daily(rows,Config(fast=20,slow=50,fee_bps=0,slippage_bps=0),evaluation_start=evaluation_start)
    _,actual,_,_=run(rows,20,50,slippage_points=0,evaluation_start=evaluation_start)
    expected_ids=[(t['entry_date'],t['exit_date'],t['entry'],t['exit']) for t in expected]
    actual_ids=[(t['entry_time'].date(),t['exit_time'].date(),t['entry'],t['exit']) for t in actual]
    if expected_ids!=actual_ids:raise AssertionError('Daily20/50 entry/exit changed versus existing calls baseline')
    report=dict(baseline_entry_exit_matches_existing_daily_engine=True,frozen_pe_baseline_verified=True,
                baseline_config_sha256=hashlib.sha256((ROOT/'strategies/nifty_daily_ema_monthly.py').read_bytes()).hexdigest(),
                baseline='1d_20_50',evaluation_start=evaluation_start,sources=source_manifest,
                excluded_incomplete_days=excluded_reference,baseline_summary=baseline,results=results,
                selection='Descriptive comparison of 32 predefined cases; no live change, no untouched holdout or optimization claim',
                option_backtest='Not available: no historical monthly-option premiums, contract rolls, bid/ask, charges or 30000 premium-cap simulation')
    (output/'comparison.json').write_text(json.dumps(report,indent=2,default=str,allow_nan=False))
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output-dir',default='outputs/nifty_bullish_ema_timeframes')
    p.add_argument('--evaluation-start',default='2021-01-01')
    args=p.parse_args();compare(args.output_dir,evaluation_start=args.evaluation_start)
