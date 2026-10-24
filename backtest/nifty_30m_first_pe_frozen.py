"""Frozen research preset PE1-V1. No live execution or tuning switches."""
import argparse
import sys

PRESET = 'PE1-V1'


def frozen_arguments(args):
    values = ['backtest.nifty_30m_reference', '--first-signal-pe',
              '--stop-points', '25', '--max-entries', '1', '--buffer-percent', '5',
              '--slippage-points', '1']
    if args.csv_path:
        values.append(args.csv_path)
    else:
        values.extend(['--database', '--from-date', args.from_date, '--to-date', args.to_date])
    if args.summary_only:
        values.append('--summary-only')
    if args.skip_incomplete_days:
        values.append('--skip-incomplete-days')
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv_path', nargs='?')
    parser.add_argument('--from-date')
    parser.add_argument('--to-date')
    parser.add_argument('--summary-only', action='store_true')
    parser.add_argument('--skip-incomplete-days', action='store_true')
    args = parser.parse_args()
    if args.csv_path:
        if args.from_date or args.to_date or args.skip_incomplete_days:
            parser.error('CSV cannot combine with date arguments or --skip-incomplete-days')
    elif not args.from_date or not args.to_date:
        parser.error('Provide CSV or both --from-date and --to-date')
    print(f'FROZEN RESEARCH PRESET: {PRESET}; live trading unchanged')
    from backtest.nifty_30m_reference import main as run_backtest
    previous_argv = sys.argv
    try:
        sys.argv = frozen_arguments(args)
        run_backtest()
    finally:
        sys.argv = previous_argv


if __name__ == '__main__':
    main()
