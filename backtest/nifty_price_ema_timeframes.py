"""Bearish price-versus-single-EMA12/36 signals across four timeframes."""
import argparse
import json
from pathlib import Path
from backtest.nifty_bearish_ema_timeframes import run_comparison,verify_baseline


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output-dir',default='outputs/nifty_price_ema_timeframes')
    p.add_argument('--evaluation-start',default='2021-01-01')
    args=p.parse_args()
    folder=Path(args.output_dir)
    results={}
    for period in (12,36):
        # EMA1 has alpha=1, hence equals each candle close exactly. This invokes
        # the audited core with a PRICE/EMA cross, not two smoothed EMA lines.
        results[str(period)]=run_comparison(1,period,folder/f'ema_{period}',
            evaluation_start=args.evaluation_start,price_cross=True)
    baseline=verify_baseline()
    lines=['# Price crossing below a single EMA: bearish signals','',
           'EMA12 and EMA36 are separate experiments. Entry: completed close crosses below its EMA. Exit: completed close crosses above it. Next candle open fills; cash otherwise.', '',
           '| Timeframe | Price below EMA | Closed trades | Win rate | Net index points | Profit factor | Marked drawdown points |',
           '| --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for timeframe in ('15m','30m','1h','1d'):
        for period in (12,36):
            r=results[str(period)]['results'][timeframe]
            s=r['stats'];fmt=lambda v:f'{v:.2f}' if v is not None else 'N/A'
            lines.append(f"| {timeframe} | {period} | {s['closed_trades']} | {fmt(s['win_rate_pct'])}% | {s['net_index_points']:+.2f} | {fmt(s['profit_factor'])} | {r['marked_drawdown_points']:.2f} |")
    lines+=['',f'Evaluation from {args.evaluation_start} through 2026-10-23; 2020 source history is indicator warmup. Same 13 incomplete source dates excluded. 0.5 adverse index points per fill.',
            'Start flat; no retrospective entry into an existing below-EMA state. The candle CLOSE must cross; a wick touching the EMA is not an entry. Hold overnight; no stop or target.',
            'Frozen 15m 9/21 baseline is unchanged and not activated. These experiments are index signals, NOT actual put-option profits. No monthly rollover, premium cap, lot sizing or option fees modeled.',
            'The engine uses EMA1 as an exact identity for candle close; it is not an additional smoothed indicator or a 12/36 crossover. Entry audits explicitly validate price below the selected single EMA.']
    combined=dict(strategy='price_cross_below_single_ema',entry='previous close >= previous EMA and current completed close < current EMA',
                  exit='previous close <= previous EMA and current completed close > current EMA',
                  frozen_baseline=baseline['name'],results=results)
    (folder/'comparison.json').write_text(json.dumps(combined,indent=2,default=str,allow_nan=False))
    (folder/'comparison.md').write_text('\n'.join(lines)+'\n')
    print('\n'.join(lines))
    print('Reports:',folder)


if __name__=='__main__':
    main()
