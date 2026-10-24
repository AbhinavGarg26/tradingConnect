"""Nifty 5-minute opening-range first-pullback research backtest (index points)."""
import argparse
import csv
import json
import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo('Asia/Kolkata')


def aggregate_minutes(rows):
    """Aggregate complete 09:15-aligned minute buckets; never fill missing minutes."""
    from backtest.snapshot_cache import exchange_time, valid_ohlc
    buckets = {}
    previous = None
    for row in rows:
        stamp = exchange_time(row['date'])
        if previous is not None and stamp <= previous:
            raise ValueError('Minute candles must be unique and chronological')
        previous = stamp
        if not valid_ohlc(row):
            raise ValueError(f'{stamp}: invalid minute OHLC')
        opening = stamp.replace(hour=9, minute=15, second=0, microsecond=0)
        elapsed = (stamp-opening).total_seconds()
        if elapsed < 0 or elapsed >= 375*60 or elapsed % 60:
            raise ValueError(f'{stamp}: expected regular-session minute opening timestamp')
        bucket = opening + timedelta(minutes=int(elapsed//300)*5)
        buckets.setdefault(bucket, []).append((stamp, row))
    result = []
    for bucket, group in buckets.items():
        if len(group) != 5 or any(stamp != bucket+timedelta(minutes=i)
                                  for i, (stamp, _) in enumerate(group)):
            raise ValueError(f'{bucket}: incomplete five-minute bucket')
        candles = [row for _, row in group]
        result.append(dict(date=bucket.isoformat(), open=candles[0]['open'],
                           high=max(float(b['high']) for b in candles),
                           low=min(float(b['low']) for b in candles), close=candles[-1]['close']))
    return result


@dataclass(frozen=True)
class Config:
    reward: float = 2.0
    tick: float = 0.05
    slippage: float = 0.5  # adverse index points on each fill, not option costs
    cost_points: float = 0.0  # extra round-trip index-point proxy cost
    use_vwap: bool = False  # futures data with genuine volume only
    qualified_breakout: bool = False
    stop_buffer_atr: float = 0.10
    max_risk_atr: float = 1.5
    selective: bool = False
    opening_direction_ratio: float = 0.60
    breakout_body_ratio: float = 0.60
    retest_band_atr: float = 0.15
    pullback_bars: int = 6
    wait_for_strong_breakout: bool = False

    def __post_init__(self):
        for name in ('reward', 'tick', 'slippage', 'cost_points', 'stop_buffer_atr', 'max_risk_atr',
                     'opening_direction_ratio', 'breakout_body_ratio', 'retest_band_atr'):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0 or (name in ('reward', 'tick') and value == 0):
                raise ValueError(f'Invalid {name}')
        if self.max_risk_atr < 0.5:
            raise ValueError('max_risk_atr must be at least 0.5')
        if not 0 <= self.opening_direction_ratio <= 1 or not 0 <= self.breakout_body_ratio <= 1:
            raise ValueError('Direction and body ratios must be between zero and one')
        if type(self.pullback_bars) is not int or self.pullback_bars < 1:
            raise ValueError('pullback_bars must be a positive integer')


def prepare(rows, config):
    """Require complete, contiguous regular sessions; timestamps denote bar opens."""
    sessions = {}
    previous = None
    for row in rows:
        stamp = datetime.fromisoformat(str(row['date']))
        stamp = stamp.replace(tzinfo=IST) if stamp.tzinfo is None else stamp.astimezone(IST)
        if previous is not None and stamp <= previous:
            raise ValueError('Input must be unique and chronological')
        previous = stamp
        bar = {'date': stamp, **{k: float(row[k]) for k in ('open', 'high', 'low', 'close')}}
        o, h, l, c = (bar[k] for k in ('open', 'high', 'low', 'close'))
        if not all(math.isfinite(v) and v > 0 for v in (o, h, l, c)) or not l <= min(o, c) <= max(o, c) <= h:
            raise ValueError('Invalid OHLC')
        if config.use_vwap:
            v = float(row['volume'])
            if not math.isfinite(v) or v <= 0:
                raise ValueError('VWAP requires positive, genuine futures volume')
            bar['volume'] = v
        sessions.setdefault(stamp.date(), []).append(bar)
    for day, bars in sessions.items():
        start = datetime.combine(day, time(9, 15), IST)
        if len(bars) != 75 or any(b['date'] != start + timedelta(minutes=5*i) for i, b in enumerate(bars)):
            raise ValueError(f'{day}: need all 75 consecutive 5-minute bars, 09:15–15:25')
    return sessions


def exit_fill(position, bar, config, *, entry_bar=False):
    """Stop first on ambiguous bars; entry-bar targets wait for later evidence."""
    s, stop, target = position['direction'], position['stop'], position['target']
    if s * (bar['low' if s == 1 else 'high'] - stop) <= 0:
        raw = stop if entry_bar else (min(stop, bar['open']) if s == 1 else max(stop, bar['open']))
        return raw - s * config.slippage, 'stop'
    if not entry_bar and s * (bar['high' if s == 1 else 'low'] - target) >= 0:
        return target - s * config.slippage, 'target'
    return None


def run(rows, config=Config(), *, diagnostics=None):
    sessions = prepare(rows, config)
    counts = Counter()
    trades = []
    ema = atr = prev_close = None
    ema_history, true_ranges = [], []
    for bars in sessions.values():
        counts['sessions'] += 1
        opening_high = max(b['high'] for b in bars[:6])
        opening_low = min(b['low'] for b in bars[:6])
        opening_move = bars[5]['close'] - bars[0]['open']
        opening_width = opening_high - opening_low
        states = {s: {'phase': 'waiting'} for s in (1, -1)}
        position = pending = None
        entries = losses = 0
        pv = volume = 0.0
        for i, b in enumerate(bars):
            t = b['date']
            # Orders submitted on the preceding close become eligible on this bar.
            if position:
                fill = ((b['open'] - position['direction'] * config.slippage, 'session_end')
                        if t.time() >= time(15, 15) else exit_fill(position, b, config))
                if fill:
                    price, reason = fill
                    points = position['direction'] * (price-position['entry']) - config.cost_points
                    trades.append({**position, 'exit_time': t, 'exit': price, 'reason': reason,
                                   'points': points, 'r': points/position['risk']})
                    losses += points <= 0
                    position = None
            if pending and not position:
                s = pending['direction']
                if i > pending['expires'] or t.time() >= time(11, 30) or entries >= 2 or losses >= 2:
                    counts['entry_order_expired'] += 1
                    pending = None
                elif s * (b['high' if s == 1 else 'low'] - pending['trigger']) >= 0:
                    raw = max(b['open'], pending['trigger']) if s == 1 else min(b['open'], pending['trigger'])
                    entry = raw + s * config.slippage
                    risk = s * (entry - pending['stop'])
                    # Recheck actual gap fill against the confirmation ATR.
                    if 0.5 * pending['atr'] <= risk <= config.max_risk_atr * pending['atr']:
                        counts['filled_entries'] += 1
                        position = {'direction': s, 'entry_time': t, 'entry': entry,
                                    'stop': pending['stop'], 'risk': risk,
                                    'target': entry + s * config.reward * risk}
                        entries += 1
                        fill = exit_fill(position, b, config, entry_bar=True)
                        if fill:
                            price, reason = fill
                            points = s*(price-entry)-config.cost_points
                            trades.append({**position, 'exit_time': t, 'exit': price,
                                           'reason': reason, 'points': points, 'r': points/risk})
                            losses += points <= 0
                            position = None
                    else:
                        counts['gap_fill_risk_rejected'] += 1
                    pending = None
            tr = max(b['high']-b['low'], abs(b['high']-prev_close), abs(b['low']-prev_close)) if prev_close else b['high']-b['low']
            prev_close = b['close']
            true_ranges.append(tr)
            if atr is None and len(true_ranges) == 14:
                atr = sum(true_ranges)/14
            elif atr is not None:
                atr = (13*atr+tr)/14
            # Session-aligned 15-minute bars: EMA is updated only at their close.
            if i % 3 == 2:
                ema = b['close'] if ema is None else ema + 2/21*(b['close']-ema)
                ema_history.append((ema, b['close']))
            if config.use_vwap:
                volume += b['volume']
                pv += (b['high']+b['low']+b['close'])/3*b['volume']
            close_time = t + timedelta(minutes=5)
            if i < 6 or close_time.time() >= time(11, 30) or len(ema_history) < 23 or atr is None or atr <= 0:
                continue
            if position or pending or entries >= 2 or losses >= 2:
                continue
            if config.selective and entries >= 1:
                continue
            for s in (1, -1):
                state = states[s]
                level = opening_high if s == 1 else opening_low
                trend = s*(ema_history[-1][1]-ema_history[-1][0]) > 0 and s*(ema_history[-1][0]-ema_history[-4][0]) > 0
                bias = not config.use_vwap or s*(b['close']-pv/volume) > 0
                if state['phase'] == 'waiting':
                    # First breakout consumes this direction, even if its filters fail.
                    if s*(b['close']-level) > 0:
                        if config.selective:
                            width = b['high'] - b['low']
                            # Frozen selectivity rules, evaluated at breakout close.
                            directional_open = opening_width > 0 and s*opening_move/opening_width >= config.opening_direction_ratio
                            strong_breakout = (width > 0 and s*(b['close']-b['open'])/width >= config.breakout_body_ratio
                                               and s*(b['close']-level) >= 0.10*atr)
                            if not directional_open or not strong_breakout:
                                counts['selective_opening_rejected' if not directional_open else 'selective_breakout_rejected'] += 1
                                if directional_open and config.wait_for_strong_breakout:
                                    continue
                                state['phase'] = 'done'
                                continue
                        if not trend or not bias:
                            counts['breakout_filter_rejections'] += 1
                            if config.qualified_breakout:
                                continue
                        counts['breakouts_accepted' if trend and bias else 'directions_consumed_by_filter'] += 1
                        state.update(phase='pullback' if trend and bias else 'done', breakout=i,
                                     touched=False, extreme=None)
                    continue
                if state['phase'] != 'pullback':
                    continue
                if i-state['breakout'] > config.pullback_bars or s*(b['close']-level) < -0.25*atr:
                    counts['pullback_expired' if i-state['breakout'] > config.pullback_bars else 'pullback_invalidated'] += 1
                    state['phase'] = 'done'
                    continue
                extreme = b['low'] if s == 1 else b['high']
                state['extreme'] = extreme if state['extreme'] is None else (min(state['extreme'], extreme) if s == 1 else max(state['extreme'], extreme))
                state['touched'] |= b['low'] <= level+config.retest_band_atr*atr and b['high'] >= level-config.retest_band_atr*atr
                width = b['high']-b['low']
                strong_close = width > 0 and ((b['high']-b['close'] <= 0.25*width) if s == 1 else (b['close']-b['low'] <= 0.25*width))
                if state['touched'] and trend and bias and s*(b['close']-level) > 0 and s*(b['close']-b['open']) > 0 and strong_close:
                    counts['confirmations'] += 1
                    trigger = (b['high'] if s == 1 else b['low']) + s*config.tick
                    stop = state['extreme']-s*config.stop_buffer_atr*atr
                    risk = s*(trigger-stop)+config.slippage
                    state['phase'] = 'done'
                    if 0.5*atr <= risk <= config.max_risk_atr*atr:
                        counts['entry_orders'] += 1
                        pending = {'direction': s, 'trigger': trigger, 'stop': stop, 'atr': atr, 'expires': i+2}
                        break
                    else:
                        counts['confirmation_risk_rejected'] += 1
        if position:
            raise ValueError('Unclosed position at session end')
    if diagnostics is not None:
        diagnostics.update(counts)
    return trades


def compare_variants(rows, config):
    """Predeclared small comparisons, no automatic winner selection or retuning."""
    from dataclasses import replace
    variants = {
        'baseline': config,
        'qualified_breakout': replace(config, qualified_breakout=True),
        'wider_stop': replace(config, stop_buffer_atr=0.25),
        'selective': replace(config, selective=True),
    }
    days = sorted(prepare(rows, config))
    split = days[max(0, int(len(days)*0.7))] if days else None
    report = {'chronological_validation_start': str(split),
              'note': 'All variants disclosed; validation is not independent if these dates were already inspected.',
              'variants': {}}
    for name, variant in variants.items():
        counts = {}
        trades = run(rows, variant, diagnostics=counts)
        development = [t for t in trades if t['entry_time'].date() < split]
        validation = [t for t in trades if t['entry_time'].date() >= split]
        report['variants'][name] = {'all': summarize(trades), 'development': summarize(development),
                                   'validation': summarize(validation), 'diagnostics': counts}
    return report


def summarize(trades):
    values = [t['points'] for t in trades]
    profit = sum(max(v, 0) for v in values)
    loss = -sum(min(v, 0) for v in values)
    equity = peak = drawdown = 0
    for v in values:
        equity += v
        peak = max(peak, equity)
        drawdown = max(drawdown, peak-equity)
    return {'trades': len(values), 'win_rate_pct': 100*sum(v > 0 for v in values)/len(values) if values else None,
            'net_index_points': sum(values), 'profit_factor': profit/loss if loss else None,
            'average_r': sum(t['r'] for t in trades)/len(trades) if trades else None,
            'max_closed_trade_drawdown_points': drawdown}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv_path', nargs='?')
    parser.add_argument('--database', '--fetch', dest='database', action='store_true',
                        help='Read Nifty 1m candles from market_snapshots and aggregate to 5m (no Kite fetch)')
    parser.add_argument('--skip-incomplete-days', action='store_true',
                        help='Explicitly exclude incomplete database sessions')
    parser.add_argument('--from-date')
    parser.add_argument('--to-date')
    parser.add_argument('--output', default='outputs/nifty_orb_pullback_trades.csv')
    parser.add_argument('--slippage', type=float, default=0.5)
    parser.add_argument('--cost-points', type=float, default=0)
    parser.add_argument('--reward', type=float, default=2)
    parser.add_argument('--variant', choices=('baseline', 'qualified_breakout', 'wider_stop', 'selective'), default='baseline')
    parser.add_argument('--compare-variants', action='store_true', help='Compare fixed variants with chronological breakdowns')
    parser.add_argument('--use-vwap', action='store_true', help='CSV futures data with genuine volume only')
    args = parser.parse_args()
    try:
        config = Config(reward=args.reward, slippage=args.slippage, cost_points=args.cost_points, use_vwap=args.use_vwap)
        from dataclasses import replace
        if args.variant == 'qualified_breakout':
            config = replace(config, qualified_breakout=True)
        elif args.variant == 'wider_stop':
            config = replace(config, stop_buffer_atr=0.25)
        elif args.variant == 'selective':
            config = replace(config, selective=True)
        if args.compare_variants and args.variant != 'baseline':
            parser.error('--compare-variants requires --variant baseline')
        excluded_days = []
        if args.skip_incomplete_days and not args.database:
            parser.error('--skip-incomplete-days requires --database')
        if args.database:
            if args.csv_path or args.use_vwap or not args.from_date or not args.to_date:
                parser.error('--database needs date range, without CSV or --use-vwap')
            start, end = (datetime.fromisoformat(v).date() for v in (args.from_date, args.to_date))
            if start > end:
                parser.error('End date precedes start')
            from backtest.snapshot_cache import load_snapshot_candles
            from sqlalchemy.exc import OperationalError
            try:
                minutes = load_snapshot_candles(start, end, database_only=True,
                    skip_incomplete_days=args.skip_incomplete_days, excluded_days=excluded_days)
            except OperationalError:
                raise ValueError('Cannot connect to the configured local database. '
                                 'Start PostgreSQL/the database container and verify DATABASE_URL; '
                                 'this uses the same configuration as the Nifty 30m reference backtest.') from None
            rows = aggregate_minutes(minutes)
            print(f'Database: market_snapshots | NIFTY 50 / 1m | {len(minutes)} minutes -> {len(rows)} five-minute candles')
            if excluded_days:
                print('Excluded incomplete days: ' + ', '.join(map(str, excluded_days)))
        else:
            if not args.csv_path:
                parser.error('Supply CSV or --database with dates')
            from pathlib import Path
            if not Path(args.csv_path).is_file():
                parser.error(
                    f'CSV not found: {args.csv_path}. Supply an existing 5-minute OHLC CSV, '
                    'or use --database --from-date YYYY-MM-DD --to-date YYYY-MM-DD.'
                )
            with open(args.csv_path, newline='') as f:
                rows = list(csv.DictReader(f))
        if not rows:
            parser.error('No complete candles available for the requested range')
        diagnostics = {}
        trades = run(rows, config, diagnostics=diagnostics)
        from pathlib import Path
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        fields = ['direction', 'entry_time', 'entry', 'stop', 'risk', 'target', 'exit_time', 'exit', 'reason', 'points', 'r']
        with path.open('w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(trades)
        print(json.dumps(summarize(trades), indent=2, allow_nan=False))
        print('Diagnostics: ' + json.dumps(diagnostics, sort_keys=True))
        if args.compare_variants:
            report = compare_variants(rows, config)
            report_path = path.with_suffix('.comparison.json')
            report_path.write_text(json.dumps(report, indent=2, allow_nan=False))
            print(json.dumps(report, indent=2, allow_nan=False))
            print(f'Comparison report: {report_path}')
        print(f'Trades: {path}\nIndex proxy only: no option returns, rupee sizing or option transaction costs modeled.')
    except (ValueError, KeyError) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    main()
