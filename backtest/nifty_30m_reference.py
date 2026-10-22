"""Backtest Nifty 30m references on consecutive one-minute OHLC."""
import argparse
import csv
from datetime import datetime, timedelta
from decimal import Decimal

from backtest.nifty_15m_breakout import TradeResult, fetch_from_kite, print_summary, summarize
from strategies.nifty_15m_breakout import Bar
from strategies.nifty_30m_reference import IST, Nifty30mReference
from strategies.nifty_30m_reference_buffered import Nifty30mReferenceBuffered
from strategies.nifty_30m_reference_ce_green import Nifty30mReferenceCEGreen
from strategies.nifty_30m_reference_step_trail import Nifty30mReferenceStepTrail


def run(rows, *, stop_points=20, max_entries=3, buffer_percent=5, slippage_points=0, reference_exit_buffer=0, ce_color_filter=False, trail_step_points=0):
    exit_buffer = Decimal(str(reference_exit_buffer))
    if not exit_buffer.is_finite() or exit_buffer < 0:
        raise ValueError('Reference exit buffer must be finite and nonnegative')
    if ce_color_filter and exit_buffer != 0:
        raise ValueError('Test CE color and reference-exit buffer separately')
    trail_step=Decimal(str(trail_step_points))
    if not trail_step.is_finite() or trail_step<0:
        raise ValueError('Trail step must be finite and nonnegative')
    if trail_step and (ce_color_filter or exit_buffer):
        raise ValueError('Test step trailing separately from other filters')
    engine_args = dict(stop_points=stop_points, max_entries=max_entries, buffer_percent=buffer_percent)
    engine = (Nifty30mReference(**engine_args) if exit_buffer == 0 else
              Nifty30mReferenceBuffered(reference_exit_buffer=exit_buffer, **engine_args))
    if ce_color_filter:
        engine = Nifty30mReferenceCEGreen(**engine_args)
    if trail_step:
        engine = Nifty30mReferenceStepTrail(trail_step_points=trail_step,**engine_args)
    slip = Decimal(str(slippage_points))
    if not slip.is_finite() or slip < 0:
        raise ValueError('Slippage must be finite and nonnegative')
    trades, signals, updates = [], [], []
    entry = None
    for row in rows:
        timestamp = datetime.fromisoformat(str(row['date']))
        timestamp = timestamp.replace(tzinfo=IST) if timestamp.tzinfo is None else timestamp.astimezone(IST)
        bar = Bar(timestamp, *(Decimal(str(row[k])) for k in ('open','high','low','close')))
        if not all(v.is_finite() and v > 0 for v in (bar.open,bar.high,bar.low,bar.close)):
            raise ValueError('Positive finite OHLC required')
        if engine.last_time and timestamp != engine.last_time + timedelta(minutes=1):
            if timestamp.date() == engine.last_time.date() or entry or engine.pending:
                raise ValueError('Missing candles or unresolved position across sessions')
        if engine.pending:
            signal = engine.pending
            direction = 1 if signal.action.endswith('CE') else -1
            if signal.action.startswith('BUY_'):
                fill = bar.open + direction * slip
                engine.confirm_fill(index_price=fill)
                entry = (signal.action[-2:], timestamp, fill, engine.stop)
            else:
                side, entered, price, stop = entry
                fill = bar.open - direction * slip
                trades.append(TradeResult(side, entered, timestamp, price, fill, stop,
                                          signal.reason, direction * (fill-price),
                                          direction * (price-stop)))
                engine.confirm_fill()
                entry = None
        signal = engine.on_bar(bar)
        if engine.reference_updates and engine.reference_updates[-1]['time'] == timestamp + timedelta(minutes=1):
            updates.append(engine.reference_updates[-1])
        if signal:
            signals.append(signal)
    return trades, entry, engine.pending, signals, updates


def print_reference_comparison(rows, **params):
    print('\nCONTROLLED COMPARISON — V3 vs V3B; only reference-exit buffer changes')
    print('variant,trades,net_points,win_rate,profit_factor,max_drawdown,worst_trade,reference_exits,reference_net,fixed_stop_exits,fixed_stop_net,open_position,pending_signal')
    results=[]
    for buffer,label in ((0,'v3'),(5,'v3b')):
        trades,entry,pending,*_=run(rows,reference_exit_buffer=buffer,**params)
        stats=summarize(trades)
        refs=[t for t in trades if t.exit_reason=='reference_cross']
        stops=[t for t in trades if t.exit_reason=='fixed_stop']
        pf=f"{stats['profit_factor']:.2f}" if stats['profit_factor'] is not None else 'N/A'
        print(f"{label},{len(trades)},{stats['net_points']:+.2f},{stats['win_rate']:.2f},{pf},"
              f"{stats['max_drawdown']:.2f},{min((t.points for t in trades),default=Decimal(0)):.2f},"
              f"{len(refs)},{sum((t.points for t in refs),Decimal(0)):+.2f},"
              f"{len(stops)},{sum((t.points for t in stops),Decimal(0)):+.2f},{entry is not None},{pending is not None}")
        results.append((label,trades))
    print('month,variant,trades,net_points')
    for month in sorted({t.entry_time.strftime('%Y-%m') for _,group in results for t in group}):
        for label,group in results:
            subset=[t for t in group if t.entry_time.strftime('%Y-%m')==month]
            print(f'{month},{label},{len(subset)},{sum((t.points for t in subset),Decimal(0)):+.2f}')
    print('Same-sample comparison; index proxies only, not option P&L or out-of-sample validation.')


def print_ce_color_comparison(rows, **params):
    print('\nCONTROLLED COMPARISON — V3 vs V3C; only CE signal-candle color filter changes')
    print('variant,trades,net_points,win_rate,profit_factor,max_drawdown,worst_trade,open_position,pending_signal')
    results=[]
    for filtered,label in ((False,'v3'),(True,'v3c')):
        trades,entry,pending,*_=run(rows,ce_color_filter=filtered,**params)
        stats=summarize(trades)
        pf=f"{stats['profit_factor']:.2f}" if stats['profit_factor'] is not None else 'N/A'
        print(f"{label},{len(trades)},{stats['net_points']:+.2f},{stats['win_rate']:.2f},{pf},"
              f"{stats['max_drawdown']:.2f},{min((t.points for t in trades),default=Decimal(0)):.2f},{entry is not None},{pending is not None}")
        results.append((label,trades))
    print('side,variant,trades,net_points')
    for side in ('CE','PE'):
        for label,group in results:
            subset=[t for t in group if t.side==side]
            print(f'{side},{label},{len(subset)},{sum((t.points for t in subset),Decimal(0)):+.2f}')
    print('month,variant,trades,net_points')
    for month in sorted({t.entry_time.strftime('%Y-%m') for _,group in results for t in group}):
        for label,group in results:
            subset=[t for t in group if t.entry_time.strftime('%Y-%m')==month]
            print(f'{month},{label},{len(subset)},{sum((t.points for t in subset),Decimal(0)):+.2f}')
    baseline,variant=results[0][1],results[1][1]
    winners=sorted([t for t in baseline if t.points>0],key=lambda t:t.points,reverse=True)
    print('BASELINE TOP FIVE WINNERS — same-entry retention and overlapping alternative trades')
    print('side,baseline_entry,baseline_points,same_entry_variant_points,overlapping_variant_entries,overlapping_variant_points')
    for original in winners[:5]:
        same=[t for t in variant if t.side==original.side and t.entry_time==original.entry_time]
        overlap=[t for t in variant if t.side==original.side and t.entry_time<original.exit_time and t.exit_time>original.entry_time]
        retained=f'{same[0].points:+.2f}' if same else 'not retained at same entry'
        print(f'{original.side},{original.entry_time},{original.points:+.2f},{retained},'
              f'{len(overlap)},{sum((t.points for t in overlap),Decimal(0)):+.2f}')
    ce_winners=[t for t in winners if t.side=='CE']
    missing=[t for t in ce_winners if not any(v.side==t.side and v.entry_time==t.entry_time for v in variant)]
    print(f'Baseline CE winners absent at same entry: {len(missing)} of {len(ce_winners)}; '
          f'baseline points {sum((t.points for t in missing),Decimal(0)):+.2f}. Delayed replacements may differ.')
    for t in missing: print(f'MISSING CE ENTRY,{t.entry_time},{t.points:+.2f}')
    print('PE rules unchanged; PE trade opportunities may change when a rejected CE leaves the strategy flat.')
    print('Same-sample comparison; index proxies only, not option P&L or out-of-sample validation.')


def print_step_trail_comparison(rows, **params):
    print('\nCONTROLLED COMPARISON — V3 vs V3D; only 10-point step trailing changes')
    print('variant,trades,net_points,win_rate,profit_factor,max_drawdown,worst_trade,trail_exits,trail_net,open_position,pending_signal')
    results=[]
    for step,label in ((0,'v3'),(10,'v3d')):
        trades,entry,pending,*_=run(rows,trail_step_points=step,**params)
        stats=summarize(trades)
        pf=f"{stats['profit_factor']:.2f}" if stats['profit_factor'] is not None else 'N/A'
        trails=[t for t in trades if t.exit_reason=='step_trail_stop']
        print(f"{label},{len(trades)},{stats['net_points']:+.2f},{stats['win_rate']:.2f},{pf},"
              f"{stats['max_drawdown']:.2f},{min((t.points for t in trades),default=Decimal(0)):.2f},"
              f"{len(trails)},{sum((t.points for t in trails),Decimal(0)):+.2f},{entry is not None},{pending is not None}")
        results.append((label,trades))
    print('month,variant,trades,net_points')
    for month in sorted({t.entry_time.strftime('%Y-%m') for _,group in results for t in group}):
        for label,group in results:
            subset=[t for t in group if t.entry_time.strftime('%Y-%m')==month]
            print(f'{month},{label},{len(subset)},{sum((t.points for t in subset),Decimal(0)):+.2f}')
    print('BASELINE TOP FIVE WINNERS — points on matching entry in step-trail variant')
    baseline,variant=results[0][1],results[1][1]
    for t in sorted(baseline,key=lambda t:t.points,reverse=True)[:5]:
        same=[v for v in variant if v.side==t.side and v.entry_time==t.entry_time]
        outcome=f'{same[0].points:+.2f}' if same else 'entry not retained'
        print(f'{t.side},{t.entry_time},baseline={t.points:+.2f},step_trail={outcome}')
    print('Closed-minute steps and next-open fills can lose money after breakeven is armed. Index proxy only.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv_path',nargs='?')
    parser.add_argument('--fetch',action='store_true')
    parser.add_argument('--from-date')
    parser.add_argument('--to-date')
    parser.add_argument('--stop-points',type=Decimal,default=Decimal(20))
    parser.add_argument('--max-entries',type=int,default=3)
    parser.add_argument('--buffer-percent',type=Decimal,default=Decimal(5))
    parser.add_argument('--slippage-points',type=Decimal,default=Decimal(0))
    parser.add_argument('--summary-only',action='store_true')
    parser.add_argument('--trail-step-points',type=Decimal,default=Decimal(0),help='0 = frozen V3; 10 = V3D step trailing')
    parser.add_argument('--compare-step-trail',action='store_true',help='Compare frozen V3 with 10-point step trailing')
    parser.add_argument('--reference-exit-buffer',type=Decimal,default=Decimal(0),help='0 = frozen V3 (default), 5 = experimental V3B')
    parser.add_argument('--ce-color-filter',action='store_true',help='Experimental V3C: only enter CE on a green signal candle')
    parser.add_argument('--compare-ce-color',action='store_true',help='Compare frozen V3 and CE-green V3C on identical data')
    parser.add_argument('--compare-reference-buffer',action='store_true',help='Run frozen V3 and 5-point V3B on identical data and settings')
    parser.add_argument('--compare-stops',help='Optional comma-separated stop sizes, e.g. 10,15,20,25,30')
    args=parser.parse_args()
    if (args.ce_color_filter or args.compare_ce_color) and (args.reference_exit_buffer != 0 or args.compare_reference_buffer):
        parser.error('Keep CE-color and reference-buffer experiments separate')
    if (args.trail_step_points or args.compare_step_trail) and (args.ce_color_filter or args.compare_ce_color or args.reference_exit_buffer or args.compare_reference_buffer):
        parser.error('Keep step-trail experiments separate from color and exit-buffer filters')
    try:
        if args.fetch:
            if args.csv_path or not args.from_date or not args.to_date:
                parser.error('--fetch requires dates without CSV')
            start,end=(datetime.fromisoformat(v).date() for v in (args.from_date,args.to_date))
            if end<start: parser.error('End precedes start')
            rows=fetch_from_kite(start,end,interval='minute')
        else:
            if not args.csv_path: parser.error('Provide one-minute CSV or --fetch with dates')
            with open(args.csv_path,newline='') as source: rows=list(csv.DictReader(source))
        if not rows: parser.error('No candles available')
        params=dict(max_entries=args.max_entries,buffer_percent=args.buffer_percent,
                    slippage_points=args.slippage_points)
        trades,entry,pending,signals,updates=run(rows,stop_points=args.stop_points,reference_exit_buffer=args.reference_exit_buffer,ce_color_filter=args.ce_color_filter,trail_step_points=args.trail_step_points,**params)
        comparisons=[]
        if args.compare_stops:
            for value in args.compare_stops.split(','):
                stop=Decimal(value.strip())
                group,opened,unfilled,*_=run(rows,stop_points=stop,reference_exit_buffer=args.reference_exit_buffer,ce_color_filter=args.ce_color_filter,trail_step_points=args.trail_step_points,**params)
                comparisons.append((stop,summarize(group),opened is not None,unfilled is not None))
    except (ValueError,KeyError,ArithmeticError) as exc:
        parser.error(str(exc))
    label=('v3d (experimental step trail)' if args.trail_step_points else
           'v3c (experimental CE-green)' if args.ce_color_filter else
           'v3 (frozen baseline)' if args.reference_exit_buffer==0 else 'v3b (experimental buffered exits)')
    print(f'NIFTY 30M BODY REFERENCE — {label} | 1-minute close checks, next-open fills')
    print(f'Reference exit buffer: {args.reference_exit_buffer} points (entry levels unchanged)')
    print(f'Initial stop {args.stop_points} | Opening body buffer {args.buffer_percent}% | Max entries {args.max_entries}')
    print('Opening eligibility: body >=20 Nifty points; no high-low range exception.')
    print('Reference: matching-color body >40 to 80: open ±5; >80: body midpoint ±10; tighten only.')
    if not args.summary_only:
        print('signal_time,action,index_close,reason,ce_reference,pe_reference,stop')
        for s in signals:
            print(f'{s.time},{s.action},{s.index_price:.2f},{s.reason},{s.ce_reference:.2f},{s.pe_reference:.2f},{s.stop if s.stop is not None else ""}')
        print('side,entry_fill_time,exit_fill_time,entry,exit,points,reason')
        for t in trades:
            print(f'{t.side},{t.entry_time},{t.exit_time},{t.entry_price:.2f},{t.exit_price:.2f},{t.points:+.2f},{t.exit_reason}')
        for u in updates: print('REFERENCE UPDATE',u)
    print_summary(trades,entry,fill_model='next-open (1-minute)',slippage_points=args.slippage_points)
    print(f'Pending signal: {pending.action if pending else "none"}')
    for reason in ('fixed_stop','reference_cross','step_trail_stop'):
        group=[t for t in trades if t.exit_reason==reason]
        print(f'{reason}: {len(group)} exits | Net {sum((t.points for t in group),Decimal(0)):+.2f}')
    if args.compare_step_trail:
        print_step_trail_comparison(rows,stop_points=args.stop_points,**params)
    if args.compare_ce_color:
        print_ce_color_comparison(rows,stop_points=args.stop_points,**params)
    if args.compare_reference_buffer:
        print_reference_comparison(rows,stop_points=args.stop_points,**params)
    if comparisons:
        print('\nSAME-SAMPLE STOP COMPARISON — not evidence of a future best stop')
        print('stop,trades,net_points,profit_factor,max_drawdown,open_position,pending_signal')
        for stop,s,opened,unfilled in comparisons:
            pf=f"{s['profit_factor']:.2f}" if s['profit_factor'] is not None else 'N/A'
            print(f"{stop},{s['total']},{s['net_points']:+.2f},{pf},{s['max_drawdown']:.2f},{opened},{unfilled}")


if __name__=='__main__':
    main()
