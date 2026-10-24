"""Bearish-only 30m EMA comparisons from stored Nifty minutes; index signals only."""
import argparse
import csv
import json
import math
from datetime import date, timedelta
from pathlib import Path

from backtest.nifty_daily_ema import save_csv
from backtest.nifty_daily_ema_switch import summarize
from backtest.snapshot_cache import exchange_time, valid_ohlc

PAIRS=((9,21),(10,30),(20,50),(20,100),(50,200))


def aggregate_session(rows,timeframe_minutes=30):
    if timeframe_minutes not in (15,30,60):
        raise ValueError('Supported session timeframes are 15, 30 and 60 minutes')
    days={}
    previous=None
    for row in rows:
        stamp=exchange_time(row['date'])
        if previous is not None and stamp <= previous:
            raise ValueError('Minutes must be unique and chronological')
        previous=stamp
        if not valid_ohlc(row):
            raise ValueError('Invalid minute OHLC')
        days.setdefault(stamp.date(),[]).append((stamp,row))
    result=[]
    for day,group in days.items():
        opening=group[0][0].replace(hour=9,minute=15,second=0,microsecond=0)
        if len(group)!=375 or any(t != opening+timedelta(minutes=i) for i,(t,_) in enumerate(group)):
            raise ValueError(f'{day}: require all 375 regular-session minutes')
        # Keep buckets within the session, including a shortened final bucket.
        for offset in range(0,375,timeframe_minutes):
            bucket=group[offset:offset+timeframe_minutes]
            values=[r for _,r in bucket]
            result.append(dict(date=bucket[0][0],duration_minutes=len(bucket),
                               open=float(values[0]['open']),close=float(values[-1]['close']),
                               high=max(float(b['high']) for b in values),low=min(float(b['low']) for b in values)))
    return result


def aggregate_30m(rows):
    return aggregate_session(rows,30)


def run(bars,fast,slow,*,evaluation_start='2021-01-01',slippage_points=0.5,timeframe_minutes=30):
    if timeframe_minutes not in (15,30,60):
        raise ValueError('Supported session timeframes are 15, 30 and 60 minutes')
    if type(fast) is not int or type(slow) is not int or not 1 <= fast < slow:
        raise ValueError('Require integer 1 <= fast < slow')
    if not math.isfinite(slippage_points) or slippage_points<0:
        raise ValueError('Slippage must be finite and nonnegative')
    previous=None
    prepared=[]
    for row in bars:
        b=dict(row)
        b['date']=exchange_time(b['date'])
        b['duration_minutes']=int(b['duration_minutes'])
        if b['duration_minutes'] not in (15,timeframe_minutes) or not valid_ohlc(b):
            raise ValueError('Invalid session candle')
        if previous is not None and b['date']<previous:
            raise ValueError('Candles must be unique, chronological and non-overlapping')
        previous=b['date']+timedelta(minutes=b['duration_minutes'])
        for k in ('open','high','low','close'): b[k]=float(b[k])
        prepared.append(b)
    bars=prepared
    start=date.fromisoformat(evaluation_start)
    first=next((i for i,b in enumerate(bars) if b['date'].date()>=start),len(bars))
    if first<slow or first>=len(bars):
        raise ValueError('Insufficient common warmup/evaluation data')
    f=s=None
    pending=position=None
    trades,signals,curve=[],[],[]
    closed=peak=drawdown=0
    for i,b in enumerate(bars):
        if pending:
            if pending['action']=='enter_put_signal':
                position=dict(side='PE',entry_signal_time=pending['time'],entry_time=b['date'],
                              entry=b['open']-slippage_points,entry_bar=i,
                              signal_fast_ema=pending['fast'],signal_slow_ema=pending['slow'])
            else:
                fill=b['open']+slippage_points
                points=position['entry']-fill
                trades.append(dict(position,exit_signal_time=pending['time'],exit_time=b['date'],exit=fill,
                                   points=points,holding_bars=i-position['entry_bar'],
                                   holding_calendar_days=(b['date']-position['entry_time']).total_seconds()/86400))
                closed+=points
                position=None
            pending=None
        pf,ps=f,s
        f=b['close'] if f is None else f+2/(fast+1)*(b['close']-f)
        s=b['close'] if s is None else s+2/(slow+1)*(b['close']-s)
        if i<first:
            continue
        cross_down=pf>=ps and f<s
        cross_up=pf<=ps and f>s
        action='enter_put_signal' if position is None and cross_down else 'exit_to_cash' if position and cross_up else None
        if action:
            pending=dict(action=action,time=b['date']+timedelta(minutes=b['duration_minutes']),fast=f,slow=s)
            signals.append(dict(pending,previous_fast=pf,previous_slow=ps,candle_open=b['date']))
        marked=closed+(position['entry']-b['close'] if position else 0)
        peak=max(peak,marked)
        drawdown=max(drawdown,peak-marked)
        curve.append(dict(time=b['date']+timedelta(minutes=b['duration_minutes']),
                          cumulative_marked_index_points=marked,drawdown_points=peak-marked,
                          position='PE_signal' if position else 'cash',fast_ema=f,slow_ema=s))
    # Reuse point statistics, then replace the daily-only holding label.
    stats=summarize([dict(t,holding_sessions=t['holding_bars']) for t in trades])
    stats['average_holding_bars']=stats.pop('average_holding_sessions')
    report=dict(fast=fast,slow=slow,timeframe=f'{timeframe_minutes}m_session_aligned',
                first_time=curve[0]['time'],last_time=curve[-1]['time'],evaluation_bars=len(curve),
                stats=stats,max_marked_drawdown_points=drawdown,total_marked_points=curve[-1]['cumulative_marked_index_points'],
                open_position=position,pending_signal=pending,
                actual_option_backtest_status='blocked_missing_historical_option_prices',
                limitations='Bearish index signal proxy, not put premiums or rupee returns. '
                            'No option fees, lot sizes, expiry rolls or premium cap modeled. '
                            '0.5 adverse index points/fill. Overnight holding; no stop/target. '
                            'Final 15:15-15:30 session candle included. Final open leg not in closed wins.')
    return report,trades,signals,curve


def compare(bars,folder,*,evaluation_start='2021-01-01',excluded=(),timeframe_minutes=30):
    folder=Path(folder)
    folder.mkdir(parents=True,exist_ok=True)
    save_csv(folder/f'candles_{timeframe_minutes}m.csv',bars)
    results={}
    lines=[f'# Bearish-only {timeframe_minutes}-minute EMA comparison','',
           'Entry: previous fast >= slow, current fast < slow. Exit: previous fast <= slow, current fast > slow. Completed candles only; next candle open fills.', '',
           '| EMA pair | Closed trades | Win rate | Net index points | Profit factor | Marked drawdown points | Average holding bars |',
           '| --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for fast,slow in PAIRS:
        report,trades,signals,curve=run(bars,fast,slow,evaluation_start=evaluation_start,timeframe_minutes=timeframe_minutes)
        report['excluded_incomplete_days']=list(map(str,excluded))
        report['entry_rule_violations']=sum(not(s['previous_fast']>=s['previous_slow'] and s['fast']<s['slow'])
                                            for s in signals if s['action']=='enter_put_signal')
        if report['entry_rule_violations']:
            raise AssertionError('Bearish entry audit failed')
        target=folder/f'{fast}_{slow}'
        target.mkdir(exist_ok=True)
        (target/'summary.json').write_text(json.dumps(report,indent=2,default=str,allow_nan=False))
        for name,values in (('trades',trades),('signals',signals),('equity_points',curve)):
            save_csv(target/f'{name}.csv',values)
        results[f'{fast}_{slow}']=report
        s=report['stats']; fmt=lambda v:f'{v:.2f}' if v is not None else 'N/A'
        lines.append(f"| {fast}/{slow} | {s['closed_trades']} | {fmt(s['win_rate_pct'])}% | {s['net_index_points']:+.2f} | {fmt(s['profit_factor'])} | {report['max_marked_drawdown_points']:.2f} | {fmt(s['average_holding_bars'])} |")
    lines+=['',f'Evaluation requested from {evaluation_start}; 2020 observations used only for EMA warmup.',
            f'Excluded incomplete source dates: {len(excluded)}. Full dates in comparison.json.',
            ('Session bars start 09:15 IST. 25 complete 15-minute candles per regular day.' if timeframe_minutes==15 else
             f'Session bars start 09:15 IST. {360//timeframe_minutes} complete {timeframe_minutes}-minute candles plus the final 15:15–15:30 candle per regular day.'),
            'Positions carry overnight until bullish crossover. All pairs start flat, with equal data and 0.5 adverse index points per fill.',
            'Results are underlying points, NOT option returns. No historical put premiums, expiry rolls, premium budget or option fees modeled. Final open legs are excluded from closed-trade statistics.',
            'All historical entry signals were audited: fast < slow, preceded by fast >= slow. No trading service/journal changed.']
    (folder/'comparison.json').write_text(json.dumps(dict(evaluation_start=evaluation_start,excluded_incomplete_days=list(map(str,excluded)),pairs=results),indent=2,default=str,allow_nan=False))
    (folder/'comparison.md').write_text('\n'.join(lines)+'\n')
    print('\n'.join(lines))
    print('Reports:',folder)
    return results


def main(*,default_timeframe=30,default_output_dir='outputs/nifty_30m_put_comparison'):
    p=argparse.ArgumentParser(description=__doc__)
    source=p.add_mutually_exclusive_group(required=True)
    source.add_argument('--database',action='store_true')
    source.add_argument('--csv',help='Previously exported session-aligned 30m candles')
    p.add_argument('--from-date',default='2020-01-01')
    p.add_argument('--to-date',default='2026-10-23')
    p.add_argument('--evaluation-start',default='2021-01-01')
    p.add_argument('--skip-incomplete-days',action='store_true')
    p.add_argument('--timeframe',type=int,choices=(15,30,60),default=default_timeframe)
    p.add_argument('--output-dir',default=default_output_dir)
    args=p.parse_args()
    excluded=[]
    if args.database:
        from backtest.snapshot_cache import load_snapshot_candles
        minutes=load_snapshot_candles(date.fromisoformat(args.from_date),date.fromisoformat(args.to_date),
            database_only=True,skip_incomplete_days=args.skip_incomplete_days,excluded_days=excluded)
        bars=aggregate_session(minutes,args.timeframe)
        del minutes
    else:
        with open(args.csv,newline='') as f: bars=list(csv.DictReader(f))
        for b in bars:
            for k in ('open','high','low','close'):b[k]=float(b[k])
    compare(bars,args.output_dir,evaluation_start=args.evaluation_start,excluded=excluded,timeframe_minutes=args.timeframe)


if __name__=='__main__':
    main()
