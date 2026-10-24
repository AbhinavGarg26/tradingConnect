"""Compare a fixed bearish EMA pair across cached 15m, 30m, 1h and daily data."""
import argparse
import csv
import hashlib
import json
from pathlib import Path

from backtest.nifty_30m_put_comparison import run as intraday_run
from backtest.nifty_daily_ema_switch import run as daily_run
from backtest.nifty_daily_ema import save_csv

ROOT=Path(__file__).resolve().parents[1]
BASELINE=ROOT/'strategies/nifty_bearish_15m_9_21_frozen.json'
SOURCES=(('15m','outputs/nifty_15m_put_comparison/candles_15m.csv','outputs/nifty_15m_put_comparison/comparison.json',15),
         ('30m','outputs/nifty_30m_put_comparison/candles_30m.csv','outputs/nifty_30m_put_comparison/comparison.json',30),
         ('1h','outputs/nifty_1h_put_comparison/candles_60m.csv','outputs/nifty_1h_put_comparison/comparison.json',60),
         ('1d','outputs/nifty_daily_ema_comparison/daily_input.csv','outputs/nifty_daily_ema_comparison/comparison.json',None))


def verify_baseline():
    baseline=json.loads(BASELINE.read_text())
    for path_key,hash_key in (('source_candles','source_candles_sha256'),('historical_trade_file','historical_trade_sha256')):
        if hashlib.sha256((ROOT/baseline[path_key]).read_bytes()).hexdigest()!=baseline[hash_key]:
            raise ValueError('Frozen baseline input/trades changed; comparison stopped')
    return baseline


def run_comparison(fast,slow,folder,*,evaluation_start='2021-01-01',price_cross=False):
    if price_cross and fast!=1:
        raise ValueError('Single-price crossover requires fast=1 (EMA1 equals candle close)')
    baseline=verify_baseline()
    folder=Path(folder)
    folder.mkdir(parents=True,exist_ok=True)
    results={}
    title=f'Price crossing below EMA{slow}' if price_cross else f'Bearish EMA {fast}/{slow}'
    lines=[f'# {title}: timeframe comparison','',
           'Frozen reference: 15m 9/21, retained unchanged. This is separate research, not a change to the calls-only monthly strategy.', '',
           '| Timeframe | Closed trades | Win rate | Net index points | Profit factor | Marked drawdown points |',
           '| --- | ---: | ---: | ---: | ---: | ---: |']
    for name,csv_path,coverage_path,minutes in SOURCES:
        with (ROOT/csv_path).open(newline='') as f:
            rows=list(csv.DictReader(f))
        metadata=json.loads((ROOT/coverage_path).read_text())
        if minutes:
            r,trades,signals,curve=intraday_run(rows,fast,slow,evaluation_start=evaluation_start,timeframe_minutes=minutes)
            stats=r['stats']; drawdown=r['max_marked_drawdown_points']
            violations=sum(not(s['previous_fast']>=s['previous_slow'] and s['fast']<s['slow'])
                           for s in signals if s['action']=='enter_put_signal')
        else:
            r,trades,signals,curve=daily_run(rows,fast=fast,slow=slow,evaluation_start=evaluation_start,puts_only=True)
            stats=r['put_side']; drawdown=r['max_daily_close_marked_drawdown_points']
            # Independently audit each daily entry against previous/current EMA values.
            f=s=None; history={}
            for b in rows:
                pf,ps=f,s
                close=float(b['close'])
                f=close if f is None else f+2/(fast+1)*(close-f)
                s=close if s is None else s+2/(slow+1)*(close-s)
                history[b['date'][:10]]=(pf,ps,f,s)
            violations=0
            for signal in signals:
                if signal['side']=='PE':
                    pf,ps,f,s=history[str(signal['date'])]
                    violations+=not(pf>=ps and f<s)
        if violations:
            raise AssertionError('Bearish crossover audit failed')
        result=dict(timeframe=name,fast=fast,slow=slow,price_cross=price_cross,stats=stats,marked_drawdown_points=drawdown,
                    entry_rule_violations=violations,detail=r,source_csv=csv_path,
                    source_csv_sha256=hashlib.sha256((ROOT/csv_path).read_bytes()).hexdigest(),
                    coverage_report=coverage_path,excluded_incomplete_days=metadata['excluded_incomplete_days'])
        results[name]=result
        target=folder/name
        target.mkdir(exist_ok=True)
        (target/'summary.json').write_text(json.dumps(result,indent=2,default=str,allow_nan=False))
        for kind,values in (('trades',trades),('signals',signals),('equity_points',curve)):
            save_csv(target/f'{kind}.csv',values)
        fmt=lambda v:f'{v:.2f}' if v is not None else 'N/A'
        lines.append(f"| {name} | {stats['closed_trades']} | {fmt(stats['win_rate_pct'])}% | {stats['net_index_points']:+.2f} | {fmt(stats['profit_factor'])} | {drawdown:.2f} |")
    ref=baseline['frozen_metrics']
    lines+=['',f"Frozen 15m 9/21 reference: {ref['closed_trades']} closed trades; {ref['win_rate_pct']:.2f}% wins; {ref['net_index_points']:+.2f} index points; PF {ref['profit_factor']:.2f}.",
            f'Evaluation starts {evaluation_start}; source ends 2026-10-23. Prior 2020 candles warm up each EMA. Same excluded source dates and 0.5 adverse index points per fill.',
            ('Bearish entry: previous close >= previous EMA, current completed close < current EMA. Exit: previous close <= previous EMA, current completed close > current EMA. Next candle/session open fills; cash otherwise; overnight holds allowed; no stop or target.' if price_cross else
             'Bearish entry: previous fast >= slow, completed current fast < slow. Exit: opposite crossover. Next candle/session open fills; cash otherwise; overnight holds allowed; no stop or target.'),
            'Final open legs are marked separately and excluded from closed-trade wins. Intraday bars align to 09:15 IST and retain the session tail.',
            'Index-point signal results only, not put-option returns. No option premiums, expiry rolls, lot sizing, premium cap or option fees modeled. Previously inspected history is research data, not independent validation.']
    report=dict(fast=fast,slow=slow,price_cross=price_cross,evaluation_start=evaluation_start,
                frozen_baseline=str(BASELINE),baseline_sha256=hashlib.sha256(BASELINE.read_bytes()).hexdigest(),results=results)
    (folder/'comparison.json').write_text(json.dumps(report,indent=2,default=str,allow_nan=False))
    (folder/'comparison.md').write_text('\n'.join(lines)+'\n')
    print('\n'.join(lines))
    print('Reports:',folder)
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--fast',type=int,default=12)
    p.add_argument('--slow',type=int,default=36)
    p.add_argument('--evaluation-start',default='2021-01-01')
    p.add_argument('--output-dir',default='outputs/nifty_bearish_12_36_timeframes')
    args=p.parse_args()
    run_comparison(args.fast,args.slow,args.output_dir,evaluation_start=args.evaluation_start)


if __name__=='__main__':
    main()
