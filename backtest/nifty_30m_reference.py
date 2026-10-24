"""Backtest Nifty 30m references on consecutive one-minute OHLC."""
import argparse
import csv
import logging
from datetime import datetime, timedelta
from decimal import Decimal

from backtest.nifty_15m_breakout import TradeResult, print_summary, summarize
from strategies.nifty_15m_breakout import Bar
from strategies.nifty_30m_reference import IST, Nifty30mReference
from strategies.nifty_30m_reference_buffered import Nifty30mReferenceBuffered
from strategies.nifty_30m_reference_ce_green import Nifty30mReferenceCEGreen
from strategies.nifty_30m_reference_step_trail import Nifty30mReferenceStepTrail
from strategies.nifty_30m_reference_candle_trail import Nifty30mReferenceCandleTrail
from strategies.nifty_30m_reference_first_pe import Nifty30mReferenceFirstPE


def run(rows, *, stop_points=20, max_entries=3, buffer_percent=5, slippage_points=0, reference_exit_buffer=0, ce_color_filter=False, trail_step_points=0, candle_trail_minutes=0, candle_trail_body_multiple=1.5, candle_trail_lookback=3, first_signal_pe=False):
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
    if candle_trail_minutes not in (0, 15, 30):
        raise ValueError('Candle trail timeframe must be 0, 15 or 30')
    if candle_trail_minutes and (trail_step or ce_color_filter or exit_buffer):
        raise ValueError('Test candle trailing separately from other experiments')
    if first_signal_pe and (max_entries != 1 or trail_step or candle_trail_minutes or ce_color_filter or exit_buffer):
        raise ValueError('First-signal PE requires one entry and unchanged baseline exits/filters')
    engine_args = dict(stop_points=stop_points, max_entries=max_entries, buffer_percent=buffer_percent)
    engine = (Nifty30mReference(**engine_args) if exit_buffer == 0 else
              Nifty30mReferenceBuffered(reference_exit_buffer=exit_buffer, **engine_args))
    if ce_color_filter:
        engine = Nifty30mReferenceCEGreen(**engine_args)
    if trail_step:
        engine = Nifty30mReferenceStepTrail(trail_step_points=trail_step,**engine_args)
    if candle_trail_minutes:
        engine = Nifty30mReferenceCandleTrail(candle_minutes=candle_trail_minutes,
            body_multiple=candle_trail_body_multiple, lookback=candle_trail_lookback, **engine_args)
    if first_signal_pe:
        engine = Nifty30mReferenceFirstPE(**engine_args)
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
    print_top_winner_retention(results[0][1], results[1][1])
    print('Closed-minute steps and next-open fills can lose money after breakeven is armed. Index proxy only.')


def print_top_winner_retention(baseline, variant):
    print('BASELINE TOP FIVE WINNERS — matching entry and retained profit')
    winners=sorted([t for t in baseline if t.points>0],key=lambda t:t.points,reverse=True)[:5]
    matched=profitable=full_profit=0
    baseline_total=variant_total=Decimal(0)
    print('side,baseline_entry,baseline_exit,baseline_points,variant_exit,variant_points,variant_exit_reason,profit_retained_pct')
    for t in winners:
        same=[v for v in variant if v.side==t.side and v.entry_time==t.entry_time]
        baseline_total+=t.points
        if same:
            v=same[0]
            matched+=1
            profitable+=v.points>0
            full_profit+=v.points>=t.points
            variant_total+=v.points
            print(f'{t.side},{t.entry_time},{t.exit_time},{t.points:+.2f},'
                  f'{v.exit_time},{v.points:+.2f},{v.exit_reason},{100*v.points/t.points:.2f}')
        else:
            print(f'{t.side},{t.entry_time},{t.exit_time},{t.points:+.2f},not retained,N/A,N/A,N/A')
    print(f'Top-winner retention: {matched}/{len(winners)} same entries; '
          f'{profitable}/{len(winners)} remain profitable; '
          f'{full_profit}/{len(winners)} preserve at least the full baseline profit.')
    retained=f'{100*variant_total/baseline_total:.2f}%' if baseline_total else 'N/A'
    print(f'Top-winner points: baseline {baseline_total:+.2f}; matched variant {variant_total:+.2f}; '
          f'aggregate retained {retained} (unmatched entries contribute zero to this diagnostic).')
    print('Matching uses side and entry time. Earlier exits can change later entries; this is not a portfolio return attribution.')


def print_candle_trail_comparison(rows, *, excluded_days=(), body_multiple=1.5, lookback=3, **params):
    print('\nCONTROLLED COMPARISON — frozen V3 vs 15m/30m large-body midpoint trails')
    print(f'Large body >= {body_multiple} x mean of previous {lookback} completed same-timeframe bodies.')
    print('Only whole candles held since bucket open qualify; midpoint must lock positive index points.')
    print('variant,trades,net_points,win_rate,profit_factor,max_drawdown,worst_trade,trail_exits,trail_net,open_position,pending_signal')
    results = []
    for minutes, label in ((0, 'v3'), (15, 'candle15'), (30, 'candle30')):
        trades, entry, pending, *_ = run(rows, candle_trail_minutes=minutes,
            candle_trail_body_multiple=body_multiple, candle_trail_lookback=lookback, **params)
        stats = summarize(trades)
        pf = f"{stats['profit_factor']:.2f}" if stats['profit_factor'] is not None else 'N/A'
        trails = [t for t in trades if t.exit_reason == 'candle_trail_stop']
        print(f"{label},{len(trades)},{stats['net_points']:+.2f},{stats['win_rate']:.2f},{pf},"
              f"{stats['max_drawdown']:.2f},{min((t.points for t in trades),default=Decimal(0)):.2f},"
              f"{len(trails)},{sum((t.points for t in trails),Decimal(0)):+.2f},{entry is not None},{pending is not None}")
        results.append((label, trades))
    for label, trades in results:
        print(f'\nPERIOD RESULTS: {label}')
        print_period_breakdowns(trades, rows, excluded_days=excluded_days)
    for label, trades in results[1:]:
        print(f'\nTOP-WINNER RETENTION: {label}')
        print_top_winner_retention(results[0][1], trades)
    print('Research comparison only. Initial stop, reference exits, entry cap and fill model remain active.')


def print_first_pe_comparison(rows, *, excluded_days=(), **params):
    print('\nCONTROLLED COMPARISON — first-signal PE selection, baseline exits unchanged')
    print('First CE signal skips the day; no waiting for a later PE. Maximum one entry/day.')
    print('variant,trades,net_points,win_rate,profit_factor,max_drawdown,expectancy')
    results=[]
    for selected,label in ((False,'v3'),(True,'first_signal_pe')):
        trades,entry,pending,*_=run(rows,first_signal_pe=selected,**params)
        stats=summarize(trades)
        pf=f"{stats['profit_factor']:.2f}" if stats['profit_factor'] is not None else 'N/A'
        print(f"{label},{len(trades)},{stats['net_points']:+.2f},{stats['win_rate']:.2f},"
              f"{pf},{stats['max_drawdown']:.2f},{stats['expectancy']:+.2f}")
        results.append((label,trades))
    if results[1][1] != [t for t in results[0][1] if t.side=='PE']:
        raise ValueError('First-PE trades differ from the baseline first-entry PE subset')
    print('Verified: selected trades exactly match baseline PE entries, fills and exits.')
    for label,trades in results:
        print(f'\nPERIOD RESULTS: {label}')
        print_period_breakdowns(trades,rows,excluded_days=excluded_days)
    print('Selection uses information available at signal time; historical comparison is not independent validation.')


def period_breakdowns(trades, rows, *, excluded_days=()):
    """Closed-trade results by exit date in IST; drawdown resets each period."""
    def local_date(value):
        stamp = datetime.fromisoformat(str(value))
        stamp = stamp.replace(tzinfo=IST) if stamp.tzinfo is None else stamp.astimezone(IST)
        return stamp.date()

    dates = [local_date(row['date']) for row in rows]
    dated_trades = [(local_date(trade.exit_time), trade) for trade in
                    sorted(trades, key=lambda trade: trade.exit_time)]
    dates.extend(day for day, _ in dated_trades)
    dates.extend(excluded_days)
    if not dates:
        return {'yearly': [], 'monthly': []}
    first, last = min(dates), max(dates)
    months = []
    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        months.append(f'{year:04d}-{month:02d}')
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    result = {}
    for label, keys, length in (
        ('yearly', [str(year) for year in range(first.year, last.year + 1)], 4),
        ('monthly', months, 7),
    ):
        records = []
        for key in keys:
            group = [trade for day, trade in dated_trades if day.isoformat()[:length] == key]
            stats = summarize(group)
            stats.update(period=key,
                         ce_net=sum((t.points for t in group if t.side == 'CE'), Decimal(0)),
                         pe_net=sum((t.points for t in group if t.side == 'PE'), Decimal(0)),
                         excluded_days=sum(day.isoformat()[:length] == key for day in excluded_days))
            records.append(stats)
        result[label] = records
    return result


def print_period_breakdowns(trades, rows, *, excluded_days=()):
    breakdowns = period_breakdowns(trades, rows, excluded_days=excluded_days)
    print('\nPeriod results use closed trades by exit date (IST); drawdown resets within each period.')
    print('Zero-trade periods are included. Partial boundary periods are not annualized.')
    for label in ('yearly', 'monthly'):
        print(f'\n{label.upper()} BREAKDOWN — NIFTY INDEX POINTS')
        print('period,trades,wins,losses,breakeven,win_rate_pct,net_points,expectancy,profit_factor,max_closed_drawdown,CE_net,PE_net,excluded_days')
        for stats in breakdowns[label]:
            pf = f"{stats['profit_factor']:.2f}" if stats['profit_factor'] is not None else 'N/A'
            print(f"{stats['period']},{stats['total']},{stats['wins']},{stats['losses']},"
                  f"{stats['breakeven']},{stats['win_rate']:.2f},{stats['net_points']:+.2f},"
                  f"{stats['expectancy']:+.2f},{pf},{stats['max_drawdown']:.2f},"
                  f"{stats['ce_net']:+.2f},{stats['pe_net']:+.2f},{stats['excluded_days']}")


def main():
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv_path',nargs='?')
    parser.add_argument('--fetch', '--database', dest='fetch', action='store_true',
                        help='Read market_snapshots; fetch and save missing NIFTY 1m candles from Kite')
    parser.add_argument('--from-date')
    parser.add_argument('--to-date')
    parser.add_argument('--skip-incomplete-days', action='store_true',
                        help='Exclude whole days with unrecoverable candle gaps and list excluded dates')
    parser.add_argument('--stop-points',type=Decimal,default=Decimal(20))
    parser.add_argument('--max-entries',type=int,default=3)
    parser.add_argument('--buffer-percent',type=Decimal,default=Decimal(5))
    parser.add_argument('--slippage-points',type=Decimal,default=Decimal(0))
    parser.add_argument('--summary-only',action='store_true')
    parser.add_argument('--trail-step-points',type=Decimal,default=Decimal(0),help='0 = frozen V3; 10 = V3D step trailing')
    parser.add_argument('--candle-trail-minutes', type=int, choices=(0,15,30), default=0)
    parser.add_argument('--compare-candle-trail', action='store_true', help='Compare baseline and 15m/30m body-midpoint trails')
    parser.add_argument('--candle-trail-body-multiple', type=Decimal, default=Decimal('1.5'))
    parser.add_argument('--candle-trail-lookback', type=int, default=3)
    parser.add_argument('--compare-step-trail',action='store_true',help='Compare frozen V3 with 10-point step trailing')
    parser.add_argument('--reference-exit-buffer',type=Decimal,default=Decimal(0),help='0 = frozen V3 (default), 5 = experimental V3B')
    parser.add_argument('--ce-color-filter',action='store_true',help='Experimental V3C: only enter CE on a green signal candle')
    parser.add_argument('--compare-ce-color',action='store_true',help='Compare frozen V3 and CE-green V3C on identical data')
    parser.add_argument('--compare-reference-buffer',action='store_true',help='Run frozen V3 and 5-point V3B on identical data and settings')
    parser.add_argument('--first-signal-pe', action='store_true', help='Only trade when the first signal is PE; CE skips the day')
    parser.add_argument('--compare-first-signal-pe', action='store_true', help='Compare baseline and first-signal PE with baseline exits')
    parser.add_argument('--compare-stops',help='Optional comma-separated stop sizes, e.g. 10,15,20,25,30')
    args=parser.parse_args()
    if args.candle_trail_minutes or args.compare_candle_trail:
        if (args.ce_color_filter or args.compare_ce_color or args.reference_exit_buffer
                or args.compare_reference_buffer or args.trail_step_points or args.compare_step_trail
                or (args.compare_candle_trail and args.candle_trail_minutes)):
            parser.error('Compare candle trailing separately from other experiments')
        if (not args.candle_trail_body_multiple.is_finite() or args.candle_trail_body_multiple < 1
                or args.candle_trail_lookback < 1):
            parser.error('Candle body multiple must be finite and >=1; lookback must be >=1')
    if args.first_signal_pe or args.compare_first_signal_pe:
        if (args.max_entries != 1 or args.ce_color_filter or args.compare_ce_color
                or args.reference_exit_buffer or args.compare_reference_buffer
                or args.trail_step_points or args.compare_step_trail or args.candle_trail_minutes
                or args.compare_candle_trail or args.compare_stops
                or (args.first_signal_pe and args.compare_first_signal_pe)):
            parser.error('First-signal PE experiment requires --max-entries 1 and no other experiment')
    excluded_days=[]
    if args.skip_incomplete_days and not args.fetch:
        parser.error('--skip-incomplete-days requires --database or --fetch')
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
            from backtest.snapshot_cache import load_snapshot_candles
            rows=load_snapshot_candles(start,end,skip_incomplete_days=args.skip_incomplete_days,
                                       excluded_days=excluded_days)
        else:
            if not args.csv_path: parser.error('Provide one-minute CSV or --fetch with dates')
            with open(args.csv_path,newline='') as source: rows=list(csv.DictReader(source))
        if not rows: parser.error('No candles available')
        params=dict(max_entries=args.max_entries,buffer_percent=args.buffer_percent,
                    slippage_points=args.slippage_points)
        trades,entry,pending,signals,updates=run(rows,first_signal_pe=args.first_signal_pe,stop_points=args.stop_points,reference_exit_buffer=args.reference_exit_buffer,ce_color_filter=args.ce_color_filter,trail_step_points=args.trail_step_points,candle_trail_minutes=args.candle_trail_minutes,
            candle_trail_body_multiple=args.candle_trail_body_multiple,candle_trail_lookback=args.candle_trail_lookback,**params)
        comparisons=[]
        if args.compare_stops:
            for value in args.compare_stops.split(','):
                stop=Decimal(value.strip())
                group,opened,unfilled,*_=run(rows,stop_points=stop,reference_exit_buffer=args.reference_exit_buffer,ce_color_filter=args.ce_color_filter,trail_step_points=args.trail_step_points,candle_trail_minutes=args.candle_trail_minutes,
            candle_trail_body_multiple=args.candle_trail_body_multiple,candle_trail_lookback=args.candle_trail_lookback,**params)
                comparisons.append((stop,summarize(group),opened is not None,unfilled is not None))
    except (ValueError,KeyError,ArithmeticError) as exc:
        parser.error(str(exc))
    label=('first-signal PE (experimental entry selection)' if args.first_signal_pe else
           f'candle{args.candle_trail_minutes} (experimental body-midpoint trail)' if args.candle_trail_minutes else
           'v3d (experimental step trail)' if args.trail_step_points else
           'v3c (experimental CE-green)' if args.ce_color_filter else
           'v3 (frozen baseline)' if args.reference_exit_buffer==0 else 'v3b (experimental buffered exits)')
    print(f'NIFTY 30M BODY REFERENCE — {label} | 1-minute close checks, next-open fills')
    if args.skip_incomplete_days:
        print(f'Data exclusions: {len(excluded_days)} incomplete trading days omitted')
        if excluded_days:
            print('Excluded dates: ' + ', '.join(day.isoformat() for day in excluded_days))
            print('Results cover retained days only; use the same exclusions when comparing variants.')
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
    for reason in ('fixed_stop','reference_cross','step_trail_stop','candle_trail_stop'):
        group=[t for t in trades if t.exit_reason==reason]
        print(f'{reason}: {len(group)} exits | Net {sum((t.points for t in group),Decimal(0)):+.2f}')
    print_period_breakdowns(trades, rows, excluded_days=excluded_days)
    if args.compare_first_signal_pe:
        print_first_pe_comparison(rows,excluded_days=excluded_days,stop_points=args.stop_points,**params)
    if args.compare_candle_trail:
        print_candle_trail_comparison(rows, excluded_days=excluded_days,
            body_multiple=args.candle_trail_body_multiple,lookback=args.candle_trail_lookback,
            stop_points=args.stop_points, **params)
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
