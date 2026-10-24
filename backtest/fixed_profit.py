"""Fixed Profit v2: next-open execution of opening-range S/R confirmations.

python3 -m backtest.fixed_profit /tmp/fixed_profit_candles.csv --compare-stops
Naive input timestamps are UTC. CSV columns date,open,high,low,close.
"""
import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from itertools import product
from pathlib import Path

from strategies.fixed_profit import FixedProfit
from strategies.nifty_15m_breakout import Bar
from strategies.nifty_opening_range import IST


def load_sessions(path):
    sessions = defaultdict(list)
    with open(path) as handle:
        for row in csv.DictReader(handle):
            stamp = datetime.fromisoformat(row['date'])
            stamp = (stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp).astimezone(IST)
            if time(9, 15) <= stamp.time() <= time(15, 29):
                sessions[stamp.date()].append(Bar(stamp, *(Decimal(row[k]) for k in ('open', 'high', 'low', 'close'))))
    complete, excluded = [], []
    for day, bars in sorted(sessions.items()):
        if (len(bars) == 375 and bars[0].time.time() == time(9, 15)
                and all(b.time - a.time == timedelta(minutes=1) for a, b in zip(bars, bars[1:]))
                and all(all(p.is_finite() and p > 0 for p in (b.open, b.high, b.low, b.close)) for b in bars)):
            complete.append((day, bars))
        else:
            excluded.append(str(day))
    return complete, excluded


def aggregate(bars):
    return Bar(bars[0].time, bars[0].open, max(b.high for b in bars),
               min(b.low for b in bars), bars[-1].close)


def run_session(bars, config):
    engine = FixedProfit(**config)
    reference = aggregate(bars[:30])
    engine.set_reference(reference, observed_at=bars[30].time)
    if not engine.eligible:
        return None
    entry = None
    details = None
    for i in range(30, len(bars)):
        bar = bars[i]
        if engine.pending and engine.pending.action.startswith('BUY_'):
            try:
                engine.confirm_entry(index_price=bar.open)
            except ValueError:
                engine.reject_signal()
            else:
                entry = bar.open
                details = {'date': str(bar.time.date()), 'side': engine.position,
                           'entry_time': bar.time.isoformat(), 'entry': float(entry),
                           'stop': float(engine.stop), 'target': float(engine.target),
                           'reference_high': float(reference.high), 'reference_low': float(reference.low)}
        if engine.position:
            direction = 1 if engine.position == 'CE' else -1
            # Liquidate at 15:15 open; do not inspect that minute's future range.
            if bar.time.time() >= time(15, 15):
                exit_price, reason = bar.open, 'session_end'
            elif (bar.low <= engine.stop if direction == 1 else bar.high >= engine.stop):
                exit_price = min(bar.open, engine.stop) if direction == 1 else max(bar.open, engine.stop)
                reason = 'stop'
            elif (bar.high >= engine.target if direction == 1 else bar.low <= engine.target):
                exit_price, reason = engine.target, 'target'
            else:
                continue
            return {**details, 'exit_bar_time': bar.time.isoformat(), 'exit': float(exit_price),
                    'exit_reason': reason, 'points': float(direction * (exit_price - entry))}
        # Completed bars anchored at 09:45, never build from future candles.
        duration = config['confirmation_minutes']
        if (i - 29) % duration == 0:
            engine.on_bar(aggregate(bars[i-duration+1:i+1]),
                          observed_at=bar.time + timedelta(minutes=1))
    if entry is not None:
        raise ValueError('Unclosed session position')
    return None


def statistics(trades, cost=0):
    pnls = [t['points'] - cost for t in trades]
    equity = peak = drawdown = 0
    for pnl in pnls:
        equity += pnl; peak = max(peak, equity); drawdown = max(drawdown, peak - equity)
    profit = sum(p for p in pnls if p > 0)
    loss = -sum(p for p in pnls if p < 0)
    return {'trades': len(pnls), 'win_pct': round(100 * sum(p > 0 for p in pnls)/len(pnls), 2) if pnls else 0,
            'net_points': round(sum(pnls), 2), 'avg_points': round(sum(pnls)/len(pnls), 3) if pnls else 0,
            'profit_factor': round(profit/loss, 3) if loss else None,
            'max_drawdown_points': round(drawdown, 2)}


def split_stats(trades):
    return {name: statistics([t for t in trades if lower <= t['date'] < upper])
            for name, lower, upper in [('train', '2020', '2024'), ('validation', '2024', '2026'),
                                        ('test', '2026', '2027')]}


def analyze(path, output, *, stop_points=15):
    sessions, excluded = load_sessions(path)
    results, histories = [], []
    for mode, duration, threshold, tolerance, follow in product(
            ('retest', 'rejection'), (1, 3, 5), (20, 40), (2, 5), (False, True)):
        config = dict(setup_mode=mode, confirmation_minutes=duration,
                      min_range_points=threshold, tolerance_points=tolerance, follow_through=follow, stop_points=stop_points)
        trades = [trade for _, bars in sessions if (trade := run_session(bars, config))]
        results.append({'config': config, **split_stats(trades)})
        histories.append(trades)
    # Choose only on training. Validation/test results do not influence selection.
    eligible = [i for i, r in enumerate(results) if r['train']['trades'] >= 100]
    chosen = max(eligible, key=lambda i: results[i]['train']['avg_points']) if eligible else None
    selected = None
    if chosen is not None:
        trades = histories[chosen]
        selected = {**results[chosen], 'all': statistics(trades),
                    'cost_sensitivity': {str(cost): statistics(trades, cost) for cost in (1, 2)},
                    'yearly': {str(year): statistics([t for t in trades if t['date'].startswith(str(year))])
                               for year in range(2020, 2027)},
                    'by_side': {side: statistics([t for t in trades if t['side'] == side]) for side in ('CE', 'PE')}}
    mode_leaders = {}
    for mode in ('retest', 'rejection'):
        indexes = [i for i in eligible if results[i]['config']['setup_mode'] == mode]
        if not indexes:
            continue
        index = max(indexes, key=lambda i: results[i]['train']['avg_points'])
        trades = histories[index]
        mode_leaders[mode] = {**results[index], 'all': statistics(trades),
                             'cost_1': statistics(trades, 1), 'cost_2': statistics(trades, 2)}
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    for mode, leader in mode_leaders.items():
        trades = histories[next(i for i, r in enumerate(results) if r['config'] == leader['config'])]
        with (output / f'{mode}_trades.csv').open('w') as handle:
            writer = csv.DictWriter(handle, fieldnames=list(trades[0]))
            writer.writeheader(); writer.writerows(trades)

    report = {'stop_points': stop_points, 'target_points': 30, 'complete_sessions': len(sessions), 'excluded_dates': excluded,
              'first_date': str(sessions[0][0]) if sessions else None,
              'last_date': str(sessions[-1][0]) if sessions else None,
              'selection_rule': 'Highest training mean gross points among >=100 training trades; no validation/test selection',
              'selected': selected, 'mode_leaders': mode_leaders, 'comparisons': results}
    (output/'analysis.json').write_text(json.dumps(report, indent=2))
    if chosen is not None:
        with (output/'trades.csv').open('w') as handle:
            writer = csv.DictWriter(handle, fieldnames=list(histories[chosen][0]))
            writer.writeheader(); writer.writerows(histories[chosen])
    print(json.dumps({k:v for k,v in report.items() if k not in {'comparisons', 'excluded_dates'}}, indent=2))
    return report


def compare_stops(path, output):
    """Hold original 10-point entry qualification fixed; compare exit distances."""
    sessions, excluded = load_sessions(path)
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    results = {}
    original_entries = None
    for stop_points in (10, 15):
        config = dict(setup_mode='retest', confirmation_minutes=5,
                      min_range_points=40, tolerance_points=2,
                      follow_through=False, stop_points=stop_points, entry_stop_points=10)
        trades = [trade for _, bars in sessions if (trade := run_session(bars, config))]
        entries = [(t['date'], t['side'], t['entry_time'], t['entry']) for t in trades]
        if original_entries is not None and entries != original_entries:
            raise ValueError('Stop-only comparison changed entry dates, sides, times, or prices')
        original_entries = entries
        results[str(stop_points)] = {'config': config, 'all': statistics(trades),
                                     'cost_1': statistics(trades, 1), 'cost_2': statistics(trades, 2),
                                     **split_stats(trades)}
        with (output / f'baseline_sl{stop_points}_trades.csv').open('w') as handle:
            if trades:
                writer = csv.DictWriter(handle, fieldnames=list(trades[0]))
                writer.writeheader(); writer.writerows(trades)
        print(stop_points, results[str(stop_points)], flush=True)
    report = {'complete_sessions': len(sessions), 'excluded_dates': excluded,
              'start': str(sessions[0][0]) if sessions else None,
              'end': str(sessions[-1][0]) if sessions else None,
              'comparison': results}
    (output / 'stop_comparison.json').write_text(json.dumps(report, indent=2))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv_path')
    parser.add_argument('--output', default='outputs/fixed-profit-sl15-fixed-entries')
    parser.add_argument('--stop-points', type=float, default=15)
    parser.add_argument('--compare-stops', action='store_true', help='Compare 10/15-point exits with unchanged entry qualification')
    args = parser.parse_args()
    if args.compare_stops:
        compare_stops(args.csv_path, args.output)
    else:
        analyze(args.csv_path, args.output, stop_points=args.stop_points)
