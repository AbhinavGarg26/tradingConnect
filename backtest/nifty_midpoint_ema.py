"""Backtest midpoint EMA strategy using completed 1m signals, next-open fills."""
import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from strategies.nifty_15m_breakout import Bar
from strategies.nifty_midpoint_ema import IST, NiftyMidpointEMA, variant_label
from backtest.nifty_15m_breakout import fetch_from_kite


@dataclass(frozen=True)
class Trade:
    side: str
    entry_time: datetime
    exit_time: datetime
    entry_price: Decimal
    exit_price: Decimal
    points: Decimal
    reason: str


def run(rows, *, buffer_percent=5, slippage_points=0, cooldown_minutes=0,
        ema_gap_points=Decimal('2.5'), logic='price_cross', candle_stop=True, entry_distance_points=3, max_entries=7, pe_doji_body_percent=10, early_doji_stop_points=None):
    slippage = Decimal(str(slippage_points))
    if not slippage.is_finite() or slippage < 0:
        raise ValueError('Slippage must be nonnegative and finite')
    engine = NiftyMidpointEMA(buffer_percent=buffer_percent, cooldown_minutes=cooldown_minutes,
                             ema_gap_points=ema_gap_points, logic=logic, candle_stop=candle_stop, entry_distance_points=entry_distance_points, max_entries=max_entries, pe_doji_body_percent=pe_doji_body_percent, early_doji_stop_points=early_doji_stop_points)
    entry = None
    trades, signals = [], []
    for row in rows:
        timestamp = datetime.fromisoformat(str(row['date']))
        timestamp = timestamp.replace(tzinfo=IST) if timestamp.tzinfo is None else timestamp.astimezone(IST)
        bar = Bar(timestamp, *(Decimal(str(row[k])) for k in ('open', 'high', 'low', 'close')))
        # Validate continuity before executing a simulated pending order.
        if engine.last_time and timestamp != engine.last_time + timedelta(minutes=1):
            if timestamp.date() == engine.last_time.date() or engine.pending or entry:
                raise ValueError('Missing candles or incomplete session with an open/pending trade')
        if engine.pending:
            signal = engine.pending
            direction = Decimal(1) if signal.action.endswith('CE') else Decimal(-1)
            if signal.action.startswith('BUY_'):
                entry = (signal.action[-2:], timestamp, bar.open + direction * slippage)
            else:
                side, entered, price = entry
                fill = bar.open - direction * slippage
                trades.append(Trade(side, entered, timestamp, price, fill,
                                    direction * (fill - price), signal.reason))
                entry = None
            engine.confirm_fill(fill_time=timestamp, index_price=entry[2] if entry is not None else None)
        signal = engine.on_bar(bar)
        if signal:
            signals.append(signal)
    return trades, entry, engine.pending, signals


def print_summary(trades, entry, pending, *, ema_gap_points=Decimal('2.5'), cooldown_minutes=0, logic='price_cross', candle_stop=True, entry_distance_points=3, max_entries=7, pe_doji_body_percent=10, early_doji_stop_points=None):
    def stats(group):
        wins = [t.points for t in group if t.points > 0]
        losses = [-t.points for t in group if t.points < 0]
        profit, loss = sum(wins, Decimal(0)), sum(losses, Decimal(0))
        equity = peak = drawdown = Decimal(0)
        for t in group:
            equity += t.points
            peak = max(peak, equity)
            drawdown = max(drawdown, peak - equity)
        n = len(group)
        return (f'{n} trades | Wins {len(wins)} | Losses {len(losses)} | '
                f'Breakeven {n-len(wins)-len(losses)} | Win rate {100*len(wins)/n if n else 0:.2f}%\n'
                f'Gross profit {profit:.2f} | Gross loss {loss:.2f} | Net {profit-loss:+.2f}\n'
                f'Average win {profit/len(wins) if wins else 0:.2f} | '
                f'Average loss {loss/len(losses) if losses else 0:.2f} | '
                f'Expectancy {(profit-loss)/n if n else 0:.2f} | '
                f'Profit factor {format(profit/loss, ".2f") if loss else "N/A"} | '
                f'Max closed-trade drawdown {drawdown:.2f}')
    print(f'NIFTY MIDPOINT EMA — {variant_label(ema_gap_points, cooldown_minutes, logic, candle_stop, entry_distance_points, pe_doji_body_percent, early_doji_stop_points)}'
          ' | 30m body reference | 1m EMA12/36')
    print(f'Next-minute open fills | Max entries/day: {max_entries if max_entries is not None else "unlimited"}')
    if logic == 'price_cross':
        print(f'Early doji stop: {early_doji_stop_points} points of index profit; first 3 completed held candles (None = disabled)')
        print(f'PE doji body threshold: {pe_doji_body_percent}% (None = disabled)')
        print(f'Previous candle body-midpoint stop: {"enabled (1m close trigger)" if candle_stop else "disabled"}')
    print(stats(trades))
    for side in ('CE', 'PE'):
        print(side + ': ' + stats([t for t in trades if t.side == side]))
    for reason in sorted({t.reason for t in trades}):
        group = [t for t in trades if t.reason == reason]
        print(f'{reason}: {len(group)} exits | Net {sum(t.points for t in group):+.2f}')
    print(f'Open position: {entry is not None} | Pending signal: {pending.action if pending else "none"}')
    print('R-multiple: N/A (no predefined stop). Index proxy; excludes option premiums, fees and taxes.')
    print(f'FINAL P&L (NIFTY INDEX POINTS, NOT ₹): {sum((t.points for t in trades), Decimal(0)):+.2f}')


def print_details(signals, trades):
    """Distinguish candle-close signals from simulated fills and realized points."""
    print('SIGNALS — completed candle closes, not fill prices')
    print('signal_time,action,nifty_close,reason,upper,lower,ema12,ema36,ema12_minus_ema36,candle_stop')
    for s in signals:
        print(f'{s.time},{s.action},{s.index_price:.2f},{s.reason},'
              f'{s.upper:.4f},{s.lower:.4f},{s.ema12:.2f},{s.ema36:.2f},'
              f'{s.ema12-s.ema36:+.6f},{format(s.stop, ".2f") if s.stop is not None else ""}')
    print('\nCOMPLETED TRADES — next-minute open fills including configured slippage')
    print('side,entry_fill_time,exit_fill_time,entry_index_fill,exit_index_fill,index_points,result,exit_reason')
    for t in trades:
        result = 'WIN' if t.points > 0 else 'LOSS' if t.points < 0 else 'BREAKEVEN'
        print(f'{t.side},{t.entry_time},{t.exit_time},{t.entry_price:.2f},'
              f'{t.exit_price:.2f},{t.points:+.2f},{result},{t.reason}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv_path', nargs='?')
    parser.add_argument('--fetch', action='store_true')
    doji_stops = parser.add_mutually_exclusive_group()
    doji_stops.add_argument('--early-doji-stop-points', type=Decimal, default=None, help='Historical opt-in early-doji stop; disabled by default')
    doji_stops.add_argument('--no-early-doji-stop', dest='early_doji_stop_points', action='store_const', const=None, help='Disable post-entry doji tightening')
    doji_options = parser.add_mutually_exclusive_group()
    doji_options.add_argument('--pe-doji-body-percent', type=Decimal, default=Decimal(10), help='Reject PE candle bodies <= this percent of range (default: 10)')
    doji_options.add_argument('--no-pe-doji-filter', dest='pe_doji_body_percent', action='store_const', const=None, help='Reproduce C6 without doji filter')
    entry_limits = parser.add_mutually_exclusive_group()
    entry_limits.add_argument('--max-entries', type=int, default=7, help='Combined CE/PE daily entry cap (default: 7)')
    entry_limits.add_argument('--no-entry-limit', dest='max_entries', action='store_const', const=None, help='Reproduce uncapped earlier versions')
    parser.add_argument('--entry-distance-points', type=Decimal, default=Decimal(3),
                        help='Required crossing-close distance from EMA36 for CE/PE (default: 3; C3: 0)')
    parser.add_argument('--no-candle-stop', action='store_true', help='Reproduce Version C2 without candle stop')
    parser.add_argument('--logic', choices=('price_cross', 'gap'), default='price_cross',
                        help='Version C price/EMA crosses (default), or older EMA-gap versions')
    parser.add_argument('--from-date')
    parser.add_argument('--to-date')
    parser.add_argument('--buffer-percent', type=Decimal, default=Decimal(5))
    parser.add_argument('--slippage-points', type=Decimal, default=Decimal(0))
    parser.add_argument('--summary-only', action='store_true')
    parser.add_argument('--cooldown-minutes', type=int, default=0,
                        help='Wait after exit fill before re-entry (Version B default: 0)')
    parser.add_argument('--ema-gap-points', type=Decimal, default=Decimal('2.5'),
                        help='Symmetric EMA entry/opposite exit gap (default: 2.5; baseline: 0)')
    args = parser.parse_args()
    try:
        if args.fetch:
            if args.csv_path or not args.from_date or not args.to_date:
                parser.error('--fetch requires dates without CSV')
            start, end = (datetime.fromisoformat(s).date() for s in (args.from_date, args.to_date))
            if end < start:
                parser.error('End date precedes start date')
            rows = fetch_from_kite(start, end, interval='minute')
        else:
            if not args.csv_path:
                parser.error('Provide a 1-minute CSV or --fetch with dates')
            with open(args.csv_path, newline='') as source:
                rows = list(csv.DictReader(source))
        if not rows:
            parser.error('No candles available')
        trades, entry, pending, signals = run(rows, buffer_percent=args.buffer_percent,
                                             slippage_points=args.slippage_points,
                                             cooldown_minutes=args.cooldown_minutes,
                                             ema_gap_points=args.ema_gap_points, logic=args.logic, candle_stop=not args.no_candle_stop, entry_distance_points=args.entry_distance_points, max_entries=args.max_entries, pe_doji_body_percent=args.pe_doji_body_percent, early_doji_stop_points=args.early_doji_stop_points)
    except (ValueError, KeyError) as exc:
        parser.error(str(exc))
    if not args.summary_only:
        print_details(signals, trades)
    print(f'Logic {args.logic} | Buffer {args.buffer_percent}% of opening body | Slippage {args.slippage_points}/side'
          f' | Cooldown {args.cooldown_minutes} minutes after exit fill'
          f' | EMA gap ±{args.ema_gap_points} points')
    if args.logic == 'price_cross':
        print('Entry: EMA36 crossing setup confirmed by distance and opening body threshold. Exit: first arm on a post-entry close in the trade direction of EMA12, then two consecutive closes against EMA12.')
        print(f'Crossing close must be at least {args.entry_distance_points} points beyond EMA36; EMA-gap parameter is unused.')
    print_summary(trades, entry, pending, ema_gap_points=args.ema_gap_points,
                  cooldown_minutes=args.cooldown_minutes, logic=args.logic, candle_stop=not args.no_candle_stop, entry_distance_points=args.entry_distance_points, max_entries=args.max_entries, pe_doji_body_percent=args.pe_doji_body_percent, early_doji_stop_points=args.early_doji_stop_points)


if __name__ == '__main__':
    main()
