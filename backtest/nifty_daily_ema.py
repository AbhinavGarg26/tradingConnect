"""Long-only daily EMA crossover: completed daily signals, next-session open fills."""
import argparse
import csv
import json
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path


@dataclass(frozen=True)
class Config:
    fast: int = 20
    slow: int = 50
    capital: float = 100000
    fee_bps: float = 5  # hypothetical cost per side, not an ETF/option tariff
    slippage_bps: float = 0

    def __post_init__(self):
        if type(self.fast) is not int or type(self.slow) is not int or not 1 <= self.fast < self.slow:
            raise ValueError('EMA periods must be integers: 1 <= fast < slow')
        if not math.isfinite(self.capital) or self.capital <= 0:
            raise ValueError('Capital must be positive and finite')
        for value in (self.fee_bps, self.slippage_bps):
            if not math.isfinite(value) or not 0 <= value < 10000:
                raise ValueError('Costs must be finite and between 0 and 10000 basis points')


def daily_from_minutes(rows):
    from backtest.snapshot_cache import exchange_time, valid_ohlc
    sessions = {}
    previous = None
    for row in rows:
        stamp = exchange_time(row['date'])
        if previous is not None and stamp <= previous:
            raise ValueError('Minute candles must be unique and chronological')
        previous = stamp
        if not valid_ohlc(row):
            raise ValueError('Invalid minute OHLC')
        sessions.setdefault(stamp.date(), []).append((stamp, row))
    daily = []
    for day, bars in sessions.items():
        opening = bars[0][0].replace(hour=9, minute=15, second=0, microsecond=0)
        if len(bars) != 375 or any(stamp != opening+timedelta(minutes=i) for i, (stamp, _) in enumerate(bars)):
            raise ValueError(f'{day}: need all 375 regular-session minute candles')
        values = [row for _, row in bars]
        daily.append(dict(date=str(day), open=float(values[0]['open']),
                          high=max(float(b['high']) for b in values),
                          low=min(float(b['low']) for b in values), close=float(values[-1]['close'])))
    return daily


def validate_daily(rows):
    from backtest.snapshot_cache import valid_ohlc
    output = []
    previous = None
    for row in rows:
        stamp = datetime.fromisoformat(str(row['date'])).date()
        if previous is not None and stamp <= previous:
            raise ValueError('Daily candles must be unique and chronological')
        previous = stamp
        if not valid_ohlc(row):
            raise ValueError('Invalid daily OHLC')
        output.append(dict(date=stamp, **{k: float(row[k]) for k in ('open', 'high', 'low', 'close')}))
    return output


def run(rows, config=Config(), *, evaluation_start=None):
    bars = validate_daily(rows)
    if len(bars) <= config.slow:
        raise ValueError('Need more daily candles than the slow EMA period')
    start_index = config.slow
    if evaluation_start is not None:
        evaluation_start = date.fromisoformat(str(evaluation_start))
        start_index = next((i for i, b in enumerate(bars) if b['date'] >= evaluation_start), len(bars))
        if start_index < config.slow or start_index >= len(bars):
            raise ValueError('Evaluation start needs slow-period prior warmup and at least one evaluation candle')
    fast = slow = None
    cash = config.capital
    quantity = 0
    position = pending = None
    trades, curve, signals = [], [], []
    peak = config.capital
    max_drawdown = 0
    fee, slip = config.fee_bps/10000, config.slippage_bps/10000
    benchmark_quantity = None
    benchmark_start = None
    for i, bar in enumerate(bars):
        if i == start_index:
            benchmark_quantity = config.capital/(bar['open']*(1+slip)*(1+fee))
            benchmark_start = bar['date']
        if pending:
            if pending['action'] == 'buy':
                fill = bar['open']*(1+slip)
                quantity = cash/(fill*(1+fee))
                position = dict(signal_date=pending['date'], entry_date=bar['date'], entry=fill,
                                entry_equity=cash, quantity=quantity, entry_index=i)
                cash = 0
            else:
                fill = bar['open']*(1-slip)
                cash = quantity*fill*(1-fee)
                trades.append({k: v for k, v in position.items() if k != 'entry_index'} | dict(
                    exit_signal_date=pending['date'], exit_date=bar['date'], exit=fill,
                    points=fill-position['entry'], pnl=cash-position['entry_equity'],
                    return_pct=100*(cash/position['entry_equity']-1),
                    holding_sessions=i-position['entry_index'],
                    holding_calendar_days=(bar['date']-position['entry_date']).days))
                quantity = 0
                position = None
            pending = None
        prior_fast, prior_slow = fast, slow
        fast = bar['close'] if fast is None else fast+2/(config.fast+1)*(bar['close']-fast)
        slow = bar['close'] if slow is None else slow+2/(config.slow+1)*(bar['close']-slow)
        if evaluation_start is not None and i < start_index:
            continue  # Indicator warmup only; each pair starts flat on the common date.
        if i >= config.slow-1:
            bullish = prior_fast <= prior_slow and fast > slow
            bearish = prior_fast >= prior_slow and fast < slow
            if not position and bullish:
                pending = dict(action='buy', date=bar['date'])
            elif position and bearish:
                pending = dict(action='sell', date=bar['date'])
            if pending:
                signals.append(dict(date=bar['date'], action=pending['action'], fast_ema=fast, slow_ema=slow))
        equity = cash+quantity*bar['close']  # marked to close, no hypothetical exit fee
        peak = max(peak, equity)
        drawdown = 100*(1-equity/peak)
        max_drawdown = max(max_drawdown, drawdown)
        curve.append(dict(date=bar['date'], close=bar['close'], fast_ema=fast, slow_ema=slow,
                          equity=equity, drawdown_pct=drawdown, invested=position is not None))
    profit = sum(max(t['pnl'], 0) for t in trades)
    loss = -sum(min(t['pnl'], 0) for t in trades)
    count = len(trades)
    report = dict(config=asdict(config), daily_sessions=len(curve), first_date=curve[0]['date'],
                  warmup_sessions=start_index if evaluation_start is not None else config.slow,
                  last_date=bars[-1]['date'], closed_trades=count,
                  win_rate_pct=100*sum(t['pnl'] > 0 for t in trades)/count if count else None,
                  profit_factor=profit/loss if loss else None,
                  closed_trade_pnl=sum(t['pnl'] for t in trades),
                  ending_marked_equity=curve[-1]['equity'],
                  total_return_pct=100*(curve[-1]['equity']/config.capital-1),
                  max_daily_close_drawdown_pct=max_drawdown,
                  average_holding_sessions=sum(t['holding_sessions'] for t in trades)/count if count else None,
                  open_position=position, pending_signal=pending,
                  buy_hold_start=benchmark_start,
                  buy_hold_marked_return_pct=100*(benchmark_quantity*bars[-1]['close']/config.capital-1),
                  limitations='Fractional-unit Nifty index proxy, not an executable ETF, futures or option backtest. '
                              'Costs are hypothetical. Open position marked to final close, not force-closed. '
                              'Daily-close drawdown excludes intraday losses. EMA seeded with first close; '
                              'slow-period warmup; no entry merely because fast EMA is already above slow.')
    return report, trades, curve, signals


def save_csv(path, rows):
    if not rows:
        path.write_text('')
        return
    with path.open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def compare_pairs(rows, config, evaluation_start, excluded, folder):
    from dataclasses import replace
    pairs = ((9, 21), (10, 30), (20, 50), (20, 100), (50, 200))
    bars = validate_daily(rows)
    if len(bars) <= 200:
        raise ValueError('Comparison needs more than 200 daily candles')
    start = evaluation_start or str(bars[200]['date'])
    results = {}
    folder.mkdir(parents=True, exist_ok=True)
    save_csv(folder/'daily_input.csv', rows)
    for fast, slow in pairs:
        label = f'{fast}_{slow}'
        variant = replace(config, fast=fast, slow=slow)
        report, trades, curve, signals = run(rows, variant, evaluation_start=start)
        report['excluded_incomplete_days'] = list(map(str, excluded))
        report['exposure_pct'] = 100*sum(b['invested'] for b in curve)/len(curve)
        years = (curve[-1]['date']-curve[0]['date']).days/365.25
        report['cagr_pct'] = 100*((report['ending_marked_equity']/config.capital)**(1/years)-1) if years > 0 else None
        results[label] = report
        pair_folder = folder/label
        pair_folder.mkdir(exist_ok=True)
        (pair_folder/'summary.json').write_text(json.dumps(report, indent=2, default=str, allow_nan=False))
        for name, values in (('trades', trades), ('equity', curve), ('signals', signals)):
            save_csv(pair_folder/f'{name}.csv', values)
    comparison = dict(evaluation_start=start, starting_policy='All pairs start flat and wait for a fresh crossover; earlier data used only for indicator warmup.',
                      excluded_incomplete_days=list(map(str, excluded)), pairs=results)
    (folder/'comparison.json').write_text(json.dumps(comparison, indent=2, default=str, allow_nan=False))
    lines = ['# Daily EMA crossover comparison', '', f'Evaluation requested from {start}. All pairs start flat, with common data and costs.', '',
             '| EMA pair | Closed trades | Win rate | Total return | CAGR | Daily-close drawdown | Profit factor | Average holding sessions |',
             '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
    for label, r in results.items():
        fmt = lambda v: f'{v:.2f}' if v is not None else 'N/A'
        lines.append(f"| {label.replace('_', '/')} | {r['closed_trades']} | {fmt(r['win_rate_pct'])}% | {fmt(r['total_return_pct'])}% | {fmt(r['cagr_pct'])}% | {fmt(r['max_daily_close_drawdown_pct'])}% | {fmt(r['profit_factor'])} | {fmt(r['average_holding_sessions'])} |")
    sample = next(iter(results.values()))
    lines += ['', f"Actual evaluation: {sample['first_date']} through {sample['last_date']}; {sample['daily_sessions']} sessions.",
              f"Common buy-and-hold marked return: {sample['buy_hold_marked_return_pct']:.2f}%.",
              f"Excluded incomplete sessions in source history: {len(excluded)}. Exact dates are recorded in comparison.json.", '',
              'Index proxy with fractional units. Not ETF, futures or option returns. Return includes marked open positions; win rate includes only closed trades. Costs are assumptions. Historical comparison is not independent validation.']
    (folder/'comparison.md').write_text('\n'.join(lines)+'\n')
    print('\n'.join(lines))
    print(f'Reports and per-pair trades: {folder}')
    return comparison


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--database', action='store_true')
    source.add_argument('--csv', help='Daily OHLC CSV')
    parser.add_argument('--from-date')
    parser.add_argument('--to-date')
    parser.add_argument('--skip-incomplete-days', action='store_true')
    parser.add_argument('--fast', type=int, default=20)
    parser.add_argument('--slow', type=int, default=50)
    parser.add_argument('--capital', type=float, default=100000)
    parser.add_argument('--fee-bps', type=float, default=5)
    parser.add_argument('--slippage-bps', type=float, default=0)
    parser.add_argument('--output-dir', default='outputs/nifty_daily_ema')
    parser.add_argument('--evaluation-start', help='Common evaluation start YYYY-MM-DD; earlier rows are warmup only')
    parser.add_argument('--compare-pairs', action='store_true', help='Compare 9/21, 10/30, 20/50, 20/100, 50/200 on a common evaluation period')
    args = parser.parse_args()
    try:
        config = Config(args.fast, args.slow, args.capital, args.fee_bps, args.slippage_bps)
        excluded = []
        if args.database:
            if not args.from_date or not args.to_date:
                parser.error('--database requires --from-date and --to-date')
            from backtest.snapshot_cache import load_snapshot_candles
            from sqlalchemy.exc import OperationalError
            try:
                minutes = load_snapshot_candles(date.fromisoformat(args.from_date), date.fromisoformat(args.to_date),
                    database_only=True, skip_incomplete_days=args.skip_incomplete_days, excluded_days=excluded)
            except OperationalError:
                raise ValueError('Cannot connect to the configured market_snapshots database') from None
            rows = daily_from_minutes(minutes)
            del minutes
        else:
            if args.skip_incomplete_days:
                parser.error('--skip-incomplete-days requires --database')
            with open(args.csv, newline='') as f:
                rows = list(csv.DictReader(f))
        if args.compare_pairs:
            compare_pairs(rows, config, args.evaluation_start, excluded, Path(args.output_dir))
            return
        report, trades, curve, signals = run(rows, config, evaluation_start=args.evaluation_start)
        report['excluded_incomplete_days'] = excluded
        report['data_source'] = 'market_snapshots NIFTY 50 / 1m aggregated to daily' if args.database else args.csv
        folder = Path(args.output_dir)
        folder.mkdir(parents=True, exist_ok=True)
        (folder/'summary.json').write_text(json.dumps(report, indent=2, default=str, allow_nan=False))
        for name, values in (('trades', trades), ('equity', curve), ('signals', signals)):
            save_csv(folder/f'{name}.csv', values)
        print(json.dumps(report, indent=2, default=str, allow_nan=False))
        print(f'Results: {folder}')
    except (ValueError, KeyError, OSError) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    main()
