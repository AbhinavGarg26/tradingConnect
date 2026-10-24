"""Controlled trade-frequency research around frozen selective_v1; index proxy only."""
import argparse
import csv
import hashlib
import json
from dataclasses import asdict, replace
from datetime import date
from pathlib import Path

from backtest.nifty_orb_pullback import Config, aggregate_minutes, prepare, run, summarize

BASELINE = Path(__file__).resolve().parents[1] / 'strategies/nifty_orb_selective_v1.json'


def frozen_config():
    return Config(**json.loads(BASELINE.read_text())['config'])


def experiments(base):
    # Fixed before reading outcomes. Do not expand this list after inspecting winners.
    return {
        'selective_v1': base,
        'opening_40pct': replace(base, opening_direction_ratio=0.40),
        'breakout_body_40pct': replace(base, breakout_body_ratio=0.40),
        'retest_030atr': replace(base, retest_band_atr=0.30),
        'pullback_12bars': replace(base, pullback_bars=12),
        'wait_strong_breakout': replace(base, wait_for_strong_breakout=True),
        'opening40_retest030': replace(base, opening_direction_ratio=0.40, retest_band_atr=0.30),
    }


def key(trade):
    return trade['direction'], str(trade['entry_time'])


def metrics(trades):
    result = summarize(trades)
    n = len(trades)
    # Descriptive Wilson interval; trading outcomes may not be independent.
    if n:
        p = sum(t['points'] > 0 for t in trades)/n
        z = 1.96
        centre = (p+z*z/(2*n))/(1+z*z/n)
        half = z*((p*(1-p)/n+z*z/(4*n*n))**0.5)/(1+z*z/n)
        result['win_rate_95pct_interval'] = [100*(centre-half), 100*(centre+half)]
    else:
        result['win_rate_95pct_interval'] = None
    return result


def research(rows, base=None):
    base = base or frozen_config()
    days = sorted(prepare(rows, base))
    if len(days) < 10:
        raise ValueError('Frequency research needs at least 10 complete sessions')
    split = days[int(len(days)*0.7)]
    report = {'sessions': len(days), 'first_session': str(days[0]), 'last_session': str(days[-1]),
              'chronological_validation_start': str(split),
              'baseline_config': asdict(base),
              'baseline_sha256': hashlib.sha256(BASELINE.read_bytes()).hexdigest(),
              'limitations': 'Previously inspected history is research data, not independent validation. '
                             'Wilson intervals assume independent outcomes. Index proxy, not option P&L. '
                             'No automatic selection. All experiments disclosed.',
              'variants': {}}
    baseline_keys = None
    trade_sets = {}
    for name, config in experiments(base).items():
        diagnostics = {}
        trades = run(rows, config, diagnostics=diagnostics)
        trade_sets[name] = trades
        if baseline_keys is None:
            baseline_keys = {key(t) for t in trades}
        current = {key(t) for t in trades}
        added = [t for t in trades if key(t) not in baseline_keys]
        groups = {
            'development': [t for t in trades if t['entry_time'].date() < split],
            'validation': [t for t in trades if t['entry_time'].date() >= split],
        }
        months = sorted({d.strftime('%Y-%m') for d in days})
        report['variants'][name] = {
            'config': asdict(config), 'all': metrics(trades),
            **{part: metrics(group) for part, group in groups.items()},
            'added_trades': metrics(added),
            'added_validation_trades': metrics([t for t in added if t['entry_time'].date() >= split]),
            'baseline_entries_retained': len(current & baseline_keys),
            'baseline_entries_missing': len(baseline_keys-current),
            'trades_per_20_sessions': len(trades)*20/len(days),
            'monthly': {m: metrics([t for t in trades if t['entry_time'].strftime('%Y-%m') == m]) for m in months},
            'yearly': {str(y): metrics([t for t in trades if t['entry_time'].year == y]) for y in sorted({d.year for d in days})},
            'diagnostics': diagnostics,
        }
    return report, trade_sets


def write_outputs(report, trade_sets, folder):
    folder.mkdir(parents=True, exist_ok=True)
    (folder/'comparison.json').write_text(json.dumps(report, indent=2, allow_nan=False))
    fields = ['direction', 'entry_time', 'entry', 'stop', 'risk', 'target', 'exit_time', 'exit', 'reason', 'points', 'r']
    for name, trades in trade_sets.items():
        with (folder/f'{name}_trades.csv').open('w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(trades)
    print('variant,trades,win_rate_pct,net_points,profit_factor,average_r,drawdown_points,validation_trades,validation_win_rate,validation_net_points,added_trades,added_win_rate')
    for name, result in report['variants'].items():
        a, v, added = (result[k] for k in ('all', 'validation', 'added_trades'))
        print(','.join(map(str, (name, a['trades'], a['win_rate_pct'], a['net_index_points'], a['profit_factor'],
                                a['average_r'], a['max_closed_trade_drawdown_points'], v['trades'],
                                v['win_rate_pct'], v['net_index_points'], added['trades'], added['win_rate_pct']))))
    print(f"Sessions: {report['sessions']} | Chronological split: {report['chronological_validation_start']}")
    print(f'Report and per-variant trades: {folder}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--database', action='store_true')
    source.add_argument('--csv', help='Complete 5-minute OHLC CSV')
    parser.add_argument('--from-date')
    parser.add_argument('--to-date')
    parser.add_argument('--skip-incomplete-days', action='store_true',
                        help='Explicitly exclude incomplete database days and record exclusions')
    parser.add_argument('--output-dir', default='outputs/nifty_orb_frequency')
    args = parser.parse_args()
    try:
        excluded_days = []
        if args.skip_incomplete_days and not args.database:
            parser.error('--skip-incomplete-days requires --database')
        if args.database:
            if not args.from_date or not args.to_date:
                parser.error('--database requires --from-date and --to-date')
            from backtest.snapshot_cache import load_snapshot_candles
            from sqlalchemy.exc import OperationalError
            try:
                minutes = load_snapshot_candles(date.fromisoformat(args.from_date), date.fromisoformat(args.to_date),
                                               database_only=True, skip_incomplete_days=args.skip_incomplete_days,
                                               excluded_days=excluded_days)
            except OperationalError:
                raise ValueError('Cannot connect to the configured database; verify the same connection used by Nifty 30m reference.') from None
            rows = aggregate_minutes(minutes)
        else:
            with open(args.csv, newline='') as f:
                rows = list(csv.DictReader(f))
        report, trades = research(rows)
        report['excluded_incomplete_days'] = list(map(str, excluded_days))
        write_outputs(report, trades, Path(args.output_dir))
    except (ValueError, KeyError, OSError) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    main()
