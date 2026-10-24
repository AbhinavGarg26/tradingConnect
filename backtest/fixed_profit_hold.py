"""Compare opening range thresholds and close-observed breakout holds on minute CSV.

CSV columns date,open,high,low,close. Naive timestamps are database UTC.
Entry at confirming minute close; exits on subsequent minute ranges, stop first
when both levels occur. No fees/slippage; one trade per complete regular session.
"""
import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo('Asia/Kolkata')


def analyze(path):
    sessions = defaultdict(list)
    with open(path) as handle:
        for row in csv.DictReader(handle):
            stamp = datetime.fromisoformat(row['date'])
            stamp = (stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp).astimezone(IST)
            if time(9, 15) <= stamp.time() <= time(15, 29):
                sessions[stamp.date()].append((stamp, *(float(row[k]) for k in ('open', 'high', 'low', 'close'))))
    complete = []
    for day, bars in sorted(sessions.items()):
        if (len(bars) == 375 and bars[0][0].time() == time(9, 15)
                and all(b[0] - a[0] == timedelta(minutes=1) for a, b in zip(bars, bars[1:]))
                and all(0 < b[3] <= min(b[1], b[4]) <= max(b[1], b[4]) <= b[2] for b in bars)):
            complete.append((day, bars))
    results = []
    for threshold in (20, 40):
        for hold in (0, 1, 2, 3, 5, 10):
            trades = []
            qualified = 0
            for day, bars in complete:
                high = max(b[2] for b in bars[:30]); low = min(b[3] for b in bars[:30])
                if high - low <= threshold:
                    continue
                qualified += 1
                candidate = since = entry = None
                direction = 0
                for i, bar in enumerate(bars[30:], 30):
                    stamp, _, hi, lo, close = bar
                    observed = stamp + timedelta(minutes=1)
                    if entry is not None:
                        stop = entry - direction * 10; target = entry + direction * 30
                        stop_hit = lo <= stop if direction == 1 else hi >= stop
                        target_hit = hi >= target if direction == 1 else lo <= target
                        if stop_hit or target_hit or observed.time() >= time(15, 15):
                            # Gaps can execute worse than the nominal stop.
                            exit_price = (min(bar[1], stop) if direction == 1 else max(bar[1], stop)) if stop_hit else target if target_hit else close
                            trades.append((str(day), direction * (exit_price - entry)))
                            break
                        continue
                    if observed.time() >= time(15, 15):
                        break
                    side = 1 if close > high else -1 if close < low else 0
                    if side == 0:
                        candidate = since = None
                        continue
                    if side != candidate:
                        candidate, since = side, i
                    if i - since >= hold:
                        direction, entry = side, close
            def stats(values):
                pnls = [p for _, p in values]; n = len(pnls)
                return {'trades': n, 'win_pct': round(100 * sum(p > 0 for p in pnls) / n, 2) if n else 0,
                        'net_points': round(sum(pnls), 2), 'avg_points': round(sum(pnls) / n, 3) if n else 0}
            results.append({'threshold': threshold, 'hold_minutes': hold,
                            'qualified_sessions': qualified, **stats(trades),
                            'before_2025': stats([(d, p) for d, p in trades if d < '2025-01-01']),
                            'from_2025': stats([(d, p) for d, p in trades if d >= '2025-01-01'])})
    return {'complete_sessions': len(complete), 'excluded_sessions': len(sessions) - len(complete),
            'start': str(complete[0][0]) if complete else None,
            'end': str(complete[-1][0]) if complete else None, 'results': results}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv_path')
    args = parser.parse_args()
    print(json.dumps(analyze(args.csv_path), indent=2))
