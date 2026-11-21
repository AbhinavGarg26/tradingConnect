"""Groww ORB research: spot-only one-minute close signals, never broker orders."""
import argparse
import csv
import math
from collections import defaultdict
from datetime import datetime, time, timedelta

from backtest.snapshot_cache import exchange_time, valid_ohlc


SETUPS = {
    'breakout': (60, 'wide'),
    'retest': (30, 'narrow'),
    'reversal': (5, None),
}


def run(rows, setup, *, slippage=0.0):
    if setup not in SETUPS or not math.isfinite(slippage) or slippage < 0:
        raise ValueError('Unknown setup or negative slippage')
    minutes, width_filter = SETUPS[setup]
    days = defaultdict(list)
    previous = None
    for row in rows:
        stamp = exchange_time(row['date'])
        if previous is not None and stamp <= previous:
            raise ValueError('Candles must be unique and chronological')
        if not valid_ohlc(row):
            raise ValueError(f'Invalid OHLC: {stamp}')
        previous = stamp
        days[stamp.date()].append((stamp, {k: float(row[k]) for k in ('open', 'high', 'low', 'close')}))
    trades = []
    for day, bars in days.items():
        start = datetime.combine(day, time(9, 15), bars[0][0].tzinfo)
        if len(bars) != 375 or any(stamp != start + timedelta(minutes=i)
                                   for i, (stamp, _) in enumerate(bars)):
            raise ValueError(f'{day}: expected every minute from 09:15 to 15:29 IST')
        opening = [bar for _, bar in bars[:minutes]]
        high, low = max(b['high'] for b in opening), min(b['low'] for b in opening)
        width = (high - low) / opening[0]['open']
        if ((width_filter == 'wide' and width <= 0.005)
                or (width_filter == 'narrow' and width > 0.005)):
            continue
        first_side = None
        first_time = None
        inside = False
        signal = None
        for stamp, bar in bars[minutes:]:
            close_time = stamp + timedelta(minutes=1)
            if close_time >= datetime.combine(day, time(15, 29), stamp.tzinfo):
                break
            side = 1 if bar['close'] > high else -1 if bar['close'] < low else 0
            if first_side is None:
                if side:
                    first_side, first_time = side, close_time
                    if setup == 'breakout' and side == 1 and close_time.time() < time(11):
                        signal = (stamp, side)
                        break
            elif setup != 'breakout':
                if side == 0:
                    inside = True
                elif setup == 'retest' and close_time - first_time > timedelta(minutes=60):
                    break
                elif inside and ((setup == 'retest' and side == first_side and close_time - first_time <= timedelta(minutes=60))
                                or (setup == 'reversal' and side == -1 and side == -first_side
                                    and close_time.time() < time(12))):
                    signal = (stamp, side)
                    break
        if signal is None:
            continue
        signal_stamp, side = signal
        idx = int((signal_stamp - start).total_seconds() // 60)
        entry_stamp, entry_bar = bars[idx + 1]
        entry = entry_bar['open'] + side * slippage
        stop = low if side == 1 else high
        # A gap beyond the opposite boundary invalidates the proposed entry.
        if side * (entry - stop) <= 0:
            continue
        exit_stamp, exit_price, reason = bars[374][0], bars[374][1]['close'], 'session_end'
        for stamp, bar in bars[idx + 1:374]:
            if side * (bar['close'] - stop) < 0:
                # One-minute close stop, executable at the following minute's open.
                following = bars[int((stamp - start).total_seconds() // 60) + 1]
                exit_stamp, exit_price, reason = following[0], following[1]['open'], 'range_stop'
                break
        exit_price -= side * slippage
        trades.append(dict(day=str(day), setup=setup, side='CE' if side == 1 else 'PE',
                           signal_time=signal_stamp.isoformat(), entry_time=entry_stamp.isoformat(),
                           exit_time=exit_stamp.isoformat(), entry=entry, exit=exit_price,
                           stop=stop, reason=reason, points=side * (exit_price - entry)))
    return trades


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv_path', nargs='?')
    parser.add_argument('--database', action='store_true')
    parser.add_argument('--from-date')
    parser.add_argument('--to-date')
    parser.add_argument('--setup', choices=tuple(SETUPS) + ('all',), default='all')
    parser.add_argument('--slippage-points', type=float, default=0)
    parser.add_argument('--output', help='CSV destination for completed trades')
    args = parser.parse_args()
    if args.database:
        if args.csv_path or not args.from_date or not args.to_date:
            parser.error('--database requires --from-date and --to-date, without CSV')
        from backtest.snapshot_cache import load_snapshot_candles
        rows = load_snapshot_candles(datetime.fromisoformat(args.from_date).date(),
                                     datetime.fromisoformat(args.to_date).date(), database_only=True)
    else:
        if not args.csv_path:
            parser.error('Provide minute CSV or --database with dates')
        with open(args.csv_path, newline='') as source:
            rows = list(csv.DictReader(source))
    trades = []
    for setup in SETUPS if args.setup == 'all' else (args.setup,):
        group = run(rows, setup, slippage=args.slippage_points)
        trades.extend(group)
        wins = sum(t['points'] > 0 for t in group)
        print(f'{setup}: trades={len(group)} wins={wins} net_index_points={sum(t["points"] for t in group):+.2f}')
    if args.output:
        with open(args.output, 'w', newline='') as dest:
            writer = csv.DictWriter(dest, fieldnames=('day', 'setup', 'side', 'signal_time', 'entry_time',
                         'exit_time', 'entry', 'exit', 'stop', 'reason', 'points'))
            writer.writeheader()
            writer.writerows(trades)


if __name__ == '__main__':
    main()
