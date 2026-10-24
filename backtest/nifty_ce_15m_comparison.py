"""Focused five-pair calls-only 15m EMA comparison; no trading changes."""
import argparse
import csv
import hashlib
import json
from pathlib import Path

from backtest.nifty_bullish_ema_timeframes import ROOT,run,period_stats
from backtest.nifty_daily_ema import save_csv

PAIRS=((9,21),(10,30),(12,36),(20,50),(50,200))
SOURCE=ROOT/'outputs/nifty_15m_put_comparison/candles_15m.csv'


def compare(output, *, timeframe="15m"):
    if timeframe not in {"15m", "30m"}:raise ValueError("Choose 15m or 30m")
    minutes=int(timeframe[:-1])
    source=ROOT/f"outputs/nifty_{timeframe}_put_comparison/candles_{timeframe}.csv"
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    with source.open() as f:rows=list(csv.DictReader(f))
    results={};table=[]
    for fast,slow in PAIRS:
        label=f'{fast}_{slow}'
        summary,trades,signals,curve=run(rows,fast,slow,timeframe=timeframe,evaluation_start='2021-01-01')
        summary['periods']=period_stats(trades)
        summary['extra_2_points_per_trade']=summary['stats']['net_index_points']-2*len(trades)
        summary['extra_5_points_per_trade']=summary['stats']['net_index_points']-5*len(trades)
        folder=output/label;folder.mkdir(exist_ok=True)
        for kind,values in [('trades',trades),('signals',signals),('equity_points',curve)]:save_csv(folder/f'{kind}.csv',values)
        (folder/'summary.json').write_text(json.dumps(summary,indent=2,default=str,allow_nan=False))
        results[label]=summary
        s=summary['stats']
        table.append(dict(ema_pair=f'{fast}/{slow}',trades=s['closed_trades'],win_rate_pct=s['win_rate_pct'],
                          net_index_points=s['net_index_points'],profit_factor=s['profit_factor'],
                          marked_drawdown_points=summary['max_marked_drawdown_points'],
                          average_holding_bars=s['average_holding_bars'],
                          extra_2_points_per_trade=summary['extra_2_points_per_trade'],
                          extra_5_points_per_trade=summary['extra_5_points_per_trade'],
                          points_2026=summary['periods']['recent']['net_index_points']))
    save_csv(output/'comparison.csv',table)
    metadata=json.loads((ROOT/f"outputs/nifty_{timeframe}_put_comparison/comparison.json").read_text())
    report=dict(reference='9_21',timeframe=timeframe,side='CE',evaluation_start='2021-01-01',
                source=str(source),source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                excluded_incomplete_days=metadata['excluded_incomplete_days'],results=results)
    (output/'comparison.json').write_text(json.dumps(report,indent=2,default=str,allow_nan=False))
    lines=[f'# Five {minutes}-minute CE EMA crossover variants','',
           f'Reference for this study: {timeframe} 9/21. The operational daily 20/50 calls strategy is unchanged.',
           'Evaluation 2021-01-01 through 2026-10-23; 2020 data is warmup. Same source dates and 13 excluded incomplete sessions in every case.','',
           '| EMA pair | Closed trades | Win rate | Net Nifty points | Profit factor | Marked DD points | Average holding bars |',
           '|---|---:|---:|---:|---:|---:|---:|']
    for r in table:
        lines.append(f"| {r['ema_pair']} | {r['trades']} | {r['win_rate_pct']:.2f}% | {r['net_index_points']:+,.2f} | {r['profit_factor']:.2f} | {r['marked_drawdown_points']:,.2f} | {r['average_holding_bars']:.1f} |")
    lines+=['','## Rules','',
            'Completed bullish crossover: previous fast <= slow, current fast > slow. Buy the next candle/session open. Exit at the next open after the opposite completed crossover; stay in cash otherwise. Calls only, no PE legs. Start flat and wait for a fresh signal. Positions hold overnight; no fixed stop/target or forced intraday square-off.',
            'EMA seed is the first close with recursive updates. Each fill has 0.5 adverse index points, so a closed roundtrip includes 1 point slippage. These are underlying signal results, NOT actual option-premium returns. Monthly expiry rolls, the premium cap, spreads, lot sizes and option fees are not simulated. Marked drawdown uses candle closes and excludes intrabar extremes. Final open positions are marked separately, not invented closed wins.','',
            '## Separate date periods','',
            '| EMA pair | 2021–2023 points | 2024–2025 points | 2026 points | Extra 2 points/trade, whole history | Extra 5 points/trade, whole history |',
            '|---|---:|---:|---:|---:|---:|']
    for label,r in results.items():
        lines.append('| '+label.replace('_','/')+' | '+' | '.join(f"{r['periods'][p]['net_index_points']:+,.2f}" for p in ('earlier','middle','recent'))+f" | {r['extra_2_points_per_trade']:+,.2f} | {r['extra_5_points_per_trade']:+,.2f} |")
    top=max(table,key=lambda r:r['net_index_points'])
    best_pf=max(table,key=lambda r:r['profit_factor'] or 0)
    best_win=max(table,key=lambda r:r['win_rate_pct'])
    lowest_dd=min(table,key=lambda r:r['marked_drawdown_points'])
    recent=results[top['ema_pair'].replace('/','_')]['periods']['recent']
    recent2=recent['net_index_points']-2*recent['closed_trades']
    recent5=recent['net_index_points']-5*recent['closed_trades']
    lines+=['','Period results attribute closed P&L by exit date; they are not complete marked account returns for each period. History has already been inspected and is not an untouched holdout.','',
            '## Interpretation','',
            f"Largest total net index points: {top['ema_pair']} ({top['net_index_points']:+,.2f}). Highest PF: {best_pf['ema_pair']} ({best_pf['profit_factor']:.2f}). Highest win rate: {best_win['ema_pair']} ({best_win['win_rate_pct']:.2f}%). Lowest sampled marked drawdown: {lowest_dd['ema_pair']} ({lowest_dd['marked_drawdown_points']:,.2f}). These are different selection objectives, not independent validation.",
            f"The full-history points leader {top['ema_pair']} has {recent['closed_trades']} closed trades in 2026, returning {recent['net_index_points']:+,.2f} points. Extra 2/5 points per trade leave {recent2:+,.2f} / {recent5:+,.2f}. Recent weakness must not be hidden by the full-history total.",
            'Retain the existing daily 20/50 operational baseline. Actual option/roll/transaction-cost testing is still required before any deployment change.','',
            '## Reproduce','', '```bash',f"python3 -m backtest.nifty_ce_{timeframe}_comparison",'```','',
            'comparison.csv contains the five-case table; each pair folder includes dated trades, signals and marked equity. No trading configuration, service or broker journal was changed.']
    (output/'comparison.md').write_text('\n'.join(lines)+'\n')
    print('\n'.join(lines[:14]))
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',default='outputs/nifty_ce_15m_five_pairs')
    args=parser.parse_args();compare(args.output_dir)
