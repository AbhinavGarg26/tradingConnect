"""Diagnose v2 retest losses and compare prespecified changes on fixed entry cohort.

Uses only completed pre-entry candles for filters. Counterfactual stop tests keep
original entries unchanged; these are exit sensitivity tests, not a wider-stop
strategy rerun. Skipping a trade skips the day, never searches later for a winner.
"""
import argparse
import csv
import json
from datetime import datetime, time
from pathlib import Path
from statistics import median

from backtest.fixed_profit import aggregate, load_sessions, run_session, statistics

CONFIG = dict(setup_mode='retest', confirmation_minutes=5, min_range_points=40,
              tolerance_points=2, follow_through=False, stop_points=10)


def exit_trade(bars, index, direction, stop_points=10, target_points=30):
    entry = float(bars[index].open)
    stop, target = entry-direction*stop_points, entry+direction*target_points
    for i in range(index, len(bars)):
        b = bars[i]; o,h,l = map(float, (b.open,b.high,b.low))
        if b.time.time() >= time(15,15):
            return direction*(o-entry), i, 'session_end', False
        stop_hit = l <= stop if direction == 1 else h >= stop
        target_hit = h >= target if direction == 1 else l <= target
        # Match published v2's conservative stop-first convention exactly.
        if stop_hit:
            price = min(o,stop) if direction == 1 else max(o,stop)
            return direction*(price-entry), i, 'stop', target_hit
        if target_hit:
            return target_points, i, 'target', False
    raise ValueError('No session exit data')


def features(bars, trade, previous):
    entry_time = datetime.fromisoformat(trade['entry_time'])
    i = next(i for i,b in enumerate(bars) if b.time == entry_time)
    direction = 1 if trade['side']=='CE' else -1
    entry = float(bars[i].open)
    candles = [aggregate(bars[j:j+5]) for j in range(0,i,5)]
    c = candles[-1]
    o,h,l,close = map(float,(c.open,c.high,c.low,c.close))
    span = h-l
    ema=[]
    for b in candles:
        ema.append(float(b.close) if not ema else float(b.close)*(2/21)+ema[-1]*(19/21))
    true_ranges = [float(b.high-b.low) if j==0 else max(float(b.high-b.low),
                    abs(float(b.high-candles[j-1].close)),abs(float(b.low-candles[j-1].close)))
                   for j,b in enumerate(candles)]
    atr = sum(true_ranges[-14:])/len(true_ranges[-14:])
    # Require 14 full same-session candles for an ATR filter, not partial warmup.
    atr_ready = len(true_ranges)>=14
    reference = trade['reference_high'] if direction==1 else trade['reference_low']
    reference = float(reference)
    closes = [float(b.close) for b in candles[6:]]
    crosses = sum((a-reference)*(b-reference)<0 for a,b in zip(closes,closes[1:]))
    obstacle = None
    if previous:
        level_distance = direction*((max(float(b.high) for b in previous) if direction==1 else
                                    min(float(b.low) for b in previous))-entry)
        obstacle = level_distance
    pnl,end,reason,ambiguous=exit_trade(bars,i,direction)
    if abs(pnl-trade['points'])>0.001:
        raise ValueError('Diagnostic exit differs from original backtest')
    pre_exit=bars[i:end]
    mfe=max([0]+[direction*(float(b.high if direction==1 else b.low)-entry) for b in pre_exit])
    remaining=[b for b in bars[end+1:] if b.time.time()<time(15,15)]
    recovered=any(direction*(float(b.high if direction==1 else b.low)-entry)>=30 for b in remaining) if reason=='stop' else False
    # Alternative execution: no original fill; require a subsequent 1m close
    # beyond the retest extreme within three minutes, then next-open fill.
    follow1_points = None
    for k in range(i, min(i+3,len(bars)-1)):
        observed = bars[k]
        adverse = l if direction==1 else h
        if (direction*(float(observed.close)-reference)<=0 or
                direction*(float(observed.low if direction==1 else observed.high)-adverse)<0):
            break
        favorable = h if direction==1 else l
        if direction*(float(observed.close)-favorable)>0:
            j=k+1; fill=float(bars[j].open)
            if (bars[j].time.time()<time(14,30) and abs(fill-reference)<=10 and
                    direction*(fill-reference)>0 and direction*(fill-direction*10-adverse)<=-1):
                follow1_points=exit_trade(bars,j,direction)[0]
            break
    return {**trade,'entry_index':i,'body_fraction':abs(close-o)/span if span else 0,
            'close_location':((close-l) if direction==1 else (h-close))/span if span else 0,
            'confirmation_range':span,'atr5':atr,'atr_ready':atr_ready,
            'ema_aligned':direction*(close-ema[-1])>0 and len(ema)>=20 and direction*(ema[-1]-ema[-4])>0,
            'crossings':crosses,'entry_distance':abs(entry-reference),
            'prior_day_room':obstacle,'early':entry_time.time()<time(12),
            'stop_minutes':end-i if reason=='stop' else None,
            'mfe_before_exit_minute':mfe,'target_later_after_stop':recovered,
            'both_hit_exit_minute':ambiguous,
            'follow1_points':follow1_points,
            'stop15_points':exit_trade(bars,i,direction,15)[0],
            'stop20_points':exit_trade(bars,i,direction,20)[0]}


def metrics(rows, pnl='points'):
    values=[dict(points=r[pnl]) for r in rows]
    return {'gross':statistics(values),'cost_1':statistics(values,1),'cost_2':statistics(values,2)}


def periods(rows,pnl='points'):
    return {name:metrics([r for r in rows if lo<=r['date']<hi],pnl)
            for name,lo,hi in [('all','2020','2027'),('train','2020','2024'),
                               ('validation','2024','2026'),('test','2026','2027')]}


def analyze(path,output):
    sessions,excluded=load_sessions(path)
    rows=[];previous=None
    for day,bars in sessions:
        trade=run_session(bars,CONFIG)
        if trade:
            # Previous session is only used when no calendar gap >4 days.
            prior=previous[1] if previous and (day-previous[0]).days<=4 else None
            rows.append(features(bars,trade,prior))
        previous=(day,bars)
    losses=[r for r in rows if r['points']<0]
    filters={
        'baseline':lambda r:True,
        'strong_close':lambda r:r['body_fraction']>=0.5 and r['close_location']>=0.75,
        'ema20_trend':lambda r:r['ema_aligned'],
        'before_noon':lambda r:r['early'],
        'avoid_chop':lambda r:r['crossings']<=2,
        'atr_compatible':lambda r:r['atr_ready'] and r['atr5']<=20,
        'near_level':lambda r:r['entry_distance']<=5,
        'prior_day_room':lambda r:r['prior_day_room'] is not None and
                                      (r['prior_day_room']<=0 or r['prior_day_room']>=32),
        'strong_and_trend':lambda r:r['body_fraction']>=0.5 and r['close_location']>=0.75 and r['ema_aligned'],
        'strong_trend_early':lambda r:r['body_fraction']>=0.5 and r['close_location']>=0.75 and r['ema_aligned'] and r['early']}
    results={name:periods([r for r in rows if rule(r)]) for name,rule in filters.items()}
    results['follow1_next_close']=periods([r for r in rows if r['follow1_points'] is not None],'follow1_points')
    results['stop15_same_entries']=periods(rows,'stop15_points')
    results['stop20_same_entries']=periods(rows,'stop20_points')
    eligible=[name for name in filters if name!='baseline' and results[name]['train']['gross']['trades']>=40]
    selected=max(eligible,key=lambda name:results[name]['train']['cost_2']['avg_points']) if eligible else None
    # Post-hoc combination after inspecting individual filter results; never
    # used for the training selection ranking above.
    exploratory = periods([r for r in rows if r['atr_ready'] and r['atr5']<=20 and r['crossings']<=2])
    report={'exploratory_combinations':{'atr_and_clean_level':exploratory},'sessions':len(sessions),'excluded_dates':excluded,'trades':len(rows),
            'losses':len(losses),'wins':len(rows)-len(losses),
            'diagnostics':{'losses_within_1_minute':sum(r['stop_minutes']<1 for r in losses),
                           'losses_within_5_minutes':sum(r['stop_minutes']<5 for r in losses),
                           'median_confirmation_range':median(r['confirmation_range'] for r in rows),
                           'median_atr5':median(r['atr5'] for r in rows),
                           'median_loss_mfe_before_exit_minute':median(r['mfe_before_exit_minute'] for r in losses),
                           'losses_later_touch_original_target':sum(r['target_later_after_stop'] for r in losses),
                           'ambiguous_stop_target_bars':sum(r['both_hit_exit_minute'] for r in rows),
                           'weak_confirmation_body_under_half':sum(r['body_fraction']<0.5 for r in rows),
                           'weak_confirmation_close_not_top_quarter':sum(r['close_location']<0.75 for r in rows)},
            'selection_rule':'Filter highest training average after 2-point cost; >=40 train trades; no combination search',
            'selected_filter':selected,'comparisons':results}
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    (output/'diagnostics.json').write_text(json.dumps(report,indent=2))
    with (output/'trade_diagnostics.csv').open('w') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    print(json.dumps({k:v for k,v in report.items() if k not in {'comparisons','excluded_dates'}},indent=2))
    for name,r in results.items():
        print(name,[(p,r[p]['gross']['trades'],r[p]['gross']['win_pct'],r[p]['gross']['net_points'],r[p]['cost_2']['net_points']) for p in ('train','validation','test')])
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv_path')
    parser.add_argument('--output',default='outputs/fixed-profit-diagnostics')
    args=parser.parse_args();analyze(args.csv_path,args.output)
