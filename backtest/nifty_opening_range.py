"""ORB v1: 15-minute close-sampled approximation with the v3 summary.

Intrabar breakout/stop ordering is unavailable in OHLC data. This deliberately
uses only observed closes after the opening range, never an invented OHLC path.
"""
import argparse
import csv
from datetime import datetime, time, timedelta
from decimal import Decimal

from backtest.nifty_15m_breakout import TradeResult, fetch_from_kite, print_summary
from strategies.nifty_15m_breakout import Bar
from strategies.nifty_opening_range import IST, NiftyOpeningRange


def run(rows, *, reference_minutes=15, max_entries=1, stop_points=5,
        target_points=30, slippage_points=0):
    slippage = Decimal(str(slippage_points))
    if not slippage.is_finite() or slippage < 0:
        raise ValueError('Slippage must be finite and nonnegative')
    engine = NiftyOpeningRange(reference_minutes=reference_minutes,
                              max_entries=max_entries, stop_points=stop_points,
                              target_points=target_points)
    trades, signals = [], []
    entry = None
    prior = None
    opening = []
    session = None
    for row in rows:
        timestamp = datetime.fromisoformat(str(row['date']))
        timestamp = timestamp.replace(tzinfo=IST) if timestamp.tzinfo is None else timestamp.astimezone(IST)
        if prior is not None and timestamp <= prior:
            raise ValueError('Candles must be unique and chronological')
        if session == timestamp.date() and timestamp - prior != timedelta(minutes=15):
            raise ValueError('Expected consecutive 15-minute candles')
        if session != timestamp.date():
            if entry is not None:
                raise ValueError('Session ended with an open trade; provide data through 15:15')
            opening = []
            session = timestamp.date()
            if timestamp.time() != time(9, 15):
                raise ValueError('Every session must begin with the 09:15 candle')
        prior = timestamp
        bar = Bar(timestamp, *(Decimal(str(row[k])) for k in ('open', 'high', 'low', 'close')))
        if not all(v.is_finite() and v > 0 for v in (bar.open, bar.high, bar.low, bar.close)):
            raise ValueError('OHLC prices must be positive and finite')
        if len(opening) < reference_minutes // 15:
            opening.append(bar)
            if len(opening) == reference_minutes // 15:
                reference = Bar(opening[0].time, opening[0].open,
                                max(b.high for b in opening), min(b.low for b in opening), bar.close)
                engine.set_reference(reference, observed_at=timestamp + timedelta(minutes=15))
            continue
        observed_at = timestamp + timedelta(minutes=15)
        decision = engine.on_tick(observed_at, bar.close)
        if decision is None:
            continue
        signals.append(decision)
        direction = Decimal(1) if decision.action.endswith('CE') else Decimal(-1)
        if decision.action.startswith('BUY'):
            fill = bar.close + direction * slippage
            # Index-only simulation: no option premium is modeled.
            engine.confirm_entry(index_price=fill, option_fill=1)
            entry = (decision, fill, engine.stop)
        else:
            first, entry_price, stop = entry
            exit_price = bar.close - direction * slippage
            trades.append(TradeResult(decision.action[-2:], first.time, decision.time,
                                      entry_price, exit_price, stop, decision.reason,
                                      direction * (exit_price - entry_price),
                                      direction * (entry_price - stop)))
            engine.confirm_exit()
            entry = None
    return trades, entry, signals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv_path', nargs='?')
    parser.add_argument('--fetch', action='store_true')
    parser.add_argument('--from-date')
    parser.add_argument('--to-date')
    parser.add_argument('--summary-only', action='store_true')
    parser.add_argument('--reference-minutes', type=int, choices=(15, 30), default=15)
    parser.add_argument('--max-entries', type=int, default=1)
    parser.add_argument('--stop-points', type=Decimal, default=Decimal(5))
    parser.add_argument('--target-points', type=Decimal, default=Decimal(30))
    parser.add_argument('--slippage-points', type=Decimal, default=Decimal(0))
    args = parser.parse_args()
    try:
        if args.fetch:
            if args.csv_path or not args.from_date or not args.to_date:
                parser.error('--fetch requires --from-date and --to-date without CSV')
            start, end = (datetime.fromisoformat(v).date() for v in (args.from_date, args.to_date))
            if end < start:
                parser.error('End date precedes start date')
            rows = fetch_from_kite(start, end)
        else:
            if not args.csv_path:
                parser.error('Provide CSV or --fetch with dates')
            with open(args.csv_path, newline='') as source:
                rows = list(csv.DictReader(source))
        if not rows:
            parser.error('No candles available')
        trades, entry, signals = run(rows, reference_minutes=args.reference_minutes,
                                    max_entries=args.max_entries, stop_points=args.stop_points,
                                    target_points=args.target_points, slippage_points=args.slippage_points)
    except (ValueError, KeyError) as exc:
        parser.error(str(exc))
    print('NIFTY OPENING RANGE BREAKOUT — v1')
    print(f'Reference {args.reference_minutes}m | Stop {args.stop_points} | Target {args.target_points}'
          f' | Max entries/day {args.max_entries}')
    print('15-minute close sampling approximation; intrabar breaches are not simulated.')
    if not args.summary_only:
        for signal in signals:
            print(f'{signal.time},{signal.action},{signal.index_price},{signal.reason}')
    print_summary(trades, entry, fill_model='15m sampled close (approximation)',
                  slippage_points=args.slippage_points)


if __name__ == '__main__':
    main()
