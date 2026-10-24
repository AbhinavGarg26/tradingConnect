"""Compare five bearish-only daily EMA signals using a cached daily index export."""
import argparse
import csv
import json
from pathlib import Path
from backtest.nifty_daily_ema import save_csv
from backtest.nifty_daily_ema_switch import run, summarize

PAIRS=((9,21),(10,30),(20,50),(20,100),(50,200))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--csv',default='outputs/nifty_daily_ema_comparison/daily_input.csv')
    p.add_argument('--source-report',default='outputs/nifty_daily_ema_comparison/comparison.json')
    p.add_argument('--evaluation-start',default='2021-01-01')
    p.add_argument('--output-dir',default='outputs/nifty_daily_put_comparison')
    args=p.parse_args()
    with open(args.csv,newline='') as f:
        rows=list(csv.DictReader(f))
    source=json.loads(Path(args.source_report).read_text()) if args.source_report else {}
    folder=Path(args.output_dir)
    folder.mkdir(parents=True,exist_ok=True)
    results={}
    lines=['# Bearish-only daily EMA comparison', '',
           'Nifty underlying signals only; NOT historical put-option returns. Entries after downward crossovers, exits after upward crossovers; both at next-session open. Cash otherwise.', '',
           '| EMA pair | Closed bearish legs | Win rate | Net index points | Profit factor | Marked drawdown points | Average holding sessions |',
           '| --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for fast,slow in PAIRS:
        report,trades,signals,curve=run(rows,evaluation_start=args.evaluation_start,fast=fast,slow=slow,puts_only=True)
        report['yearly_closed_legs']={str(year):summarize([t for t in trades if t['exit_date'].year==year])
                                      for year in sorted({b['date'].year for b in curve})}
        report['excluded_incomplete_days']=source.get('excluded_incomplete_days')
        report['data_source']=args.csv
        report['coverage_metadata']=args.source_report
        results[f'{fast}_{slow}']=report
        target=folder/f'{fast}_{slow}'
        target.mkdir(exist_ok=True)
        (target/'summary.json').write_text(json.dumps(report,indent=2,default=str,allow_nan=False))
        for name,values in (('trades',trades),('signals',signals),('equity_points',curve)):
            save_csv(target/f'{name}.csv',values)
        s=report['put_side']
        fmt=lambda v: f'{v:.2f}' if v is not None else 'N/A'
        lines.append(f"| {fast}/{slow} | {s['closed_trades']} | {fmt(s['win_rate_pct'])}% | {s['net_index_points']:+.2f} | {fmt(s['profit_factor'])} | {report['max_daily_close_marked_drawdown_points']:.2f} | {fmt(s['average_holding_sessions'])} |")
    sample=next(iter(results.values()))
    comparison=dict(evaluation_start=str(sample['first_date']),evaluation_end=str(sample['last_date']),sessions=sample['sessions'],
                    puts_only=True,data_source=args.csv,source_report=args.source_report,
                    excluded_incomplete_days=source.get('excluded_incomplete_days'),pairs=results,
                    actual_option_backtest_status='blocked_missing_historical_option_prices')
    (folder/'comparison.json').write_text(json.dumps(comparison,indent=2,default=str,allow_nan=False))
    lines += ['',f"Common evaluation: {sample['first_date']} to {sample['last_date']}; {sample['sessions']} sessions. Prior 2020 data is indicator warmup.",
              'All accounts start flat and wait for a fresh bearish crossover. Adverse slippage: 0.5 index points per fill. No option charges or account sizing modeled.',
              'Final open bearish legs are marked in the point curve and excluded from closed-leg win rates. Thirteen incomplete source dates are recorded in the source export metadata.',
              'Do not treat these figures as returns from buying monthly puts. Historical premiums, expiry rolls, dated lot sizes and the 30000 premium cap remain untested. No live strategy or paper journal is changed.']
    (folder/'comparison.md').write_text('\n'.join(lines)+'\n')
    print('\n'.join(lines))
    print('Reports:',folder)


if __name__=='__main__':
    main()
