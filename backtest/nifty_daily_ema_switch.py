"""20/50 daily CE/PE directional signal backtest. NOT option-premium performance."""
import argparse
import csv
import json
import math
from datetime import date
from pathlib import Path

from backtest.nifty_daily_ema import daily_from_minutes, save_csv, validate_daily


def summarize(trades):
    winners = [t['points'] for t in trades if t['points'] > 0]
    losses = [-t['points'] for t in trades if t['points'] < 0]
    equity = peak = drawdown = 0
    for trade in trades:
        equity += trade['points']
        peak = max(peak, equity)
        drawdown = max(drawdown, peak-equity)
    return dict(closed_trades=len(trades), wins=len(winners), losses=len(losses),
                win_rate_pct=100*len(winners)/len(trades) if trades else None,
                net_index_points=sum(t['points'] for t in trades),
                profit_factor=sum(winners)/sum(losses) if losses else None,
                average_holding_sessions=sum(t['holding_sessions'] for t in trades)/len(trades) if trades else None,
                max_closed_trade_drawdown_points=drawdown)


def run(rows, *, evaluation_start='2021-01-01', fast=20, slow=50, slippage_points=0.5, puts_only=False):
    bars = validate_daily(rows)
    if type(fast) is not int or type(slow) is not int or not 1 <= fast < slow:
        raise ValueError('Require integer periods 1 <= fast < slow')
    if not math.isfinite(slippage_points) or slippage_points < 0:
        raise ValueError('Slippage must be finite and nonnegative')
    start = date.fromisoformat(evaluation_start)
    first = next((i for i,b in enumerate(bars) if b['date'] >= start),len(bars))
    if first < slow or first >= len(bars):
        raise ValueError('Need slow-period prior history and evaluation candles')
    f = s = None
    pending = position = None
    trades, signals, curve = [], [], []
    closed = peak = max_drawdown = 0
    for i, b in enumerate(bars):
        if pending:
            direction = pending['direction']
            if position:
                old = position['direction']
                fill = b['open']-old*slippage_points
                points = old*(fill-position['entry'])
                trade = dict(position, exit_signal_date=pending['date'], exit_date=b['date'], exit=fill,
                             points=points, directional_move_pct=100*points/position['entry'],
                             holding_sessions=i-position['entry_index'])
                trades.append(trade)
                closed += points
            position = dict(direction=direction, side='CE' if direction == 1 else 'PE',
                            entry_signal_date=pending['date'], entry_date=b['date'],
                            entry=b['open']+direction*slippage_points, entry_index=i) if not puts_only or direction == -1 else None
            pending = None
        prior_f, prior_s = f, s
        f = b['close'] if f is None else f+2/(fast+1)*(b['close']-f)
        s = b['close'] if s is None else s+2/(slow+1)*(b['close']-s)
        if i < first:
            continue
        direction = 1 if prior_f <= prior_s and f > s else -1 if prior_f >= prior_s and f < s else None
        if direction is not None and (not puts_only or direction == -1 or position is not None):
            pending = dict(direction=direction,date=b['date'])
            signals.append(dict(date=b['date'], side='CE' if direction==1 else 'PE',fast_ema=f,slow_ema=s))
        marked = closed+(position['direction']*(b['close']-position['entry']) if position else 0)
        peak = max(peak,marked)
        max_drawdown = max(max_drawdown,peak-marked)
        curve.append(dict(date=b['date'], index_close=b['close'], cumulative_marked_points=marked,
                          drawdown_points=peak-marked, side=position['side'] if position else 'flat'))
    report = dict(type='underlying_directional_signal_proxy', fast=fast, slow=slow, puts_only=puts_only,
                  first_date=curve[0]['date'],last_date=curve[-1]['date'],sessions=len(curve),
                  slippage_index_points_per_fill=slippage_points,
                  combined=summarize(trades),call_side=summarize([t for t in trades if t['side']=='CE']),
                  put_side=summarize([t for t in trades if t['side']=='PE']),
                  max_daily_close_marked_drawdown_points=max_drawdown,
                  total_marked_index_points=curve[-1]['cumulative_marked_points'],
                  open_signal_position=position,pending_signal=pending,
                  actual_monthly_option_backtest_status='blocked_missing_historical_option_prices',
                  limitations='CE means bullish underlying signal; PE means bearish underlying signal. '
                              'No option prices, strikes, expiry rolls, lot sizing, premium cap, option fees or rupee returns modeled. '
                              'A profitable bearish index move is not proof a put made a profit. '
                              'Start flat, wait for fresh cross; next-open entries/exits; puts-only mode stays in cash after bullish exits; final open leg marked separately.')
    return report,trades,signals,curve


def main():
    p=argparse.ArgumentParser(description=__doc__)
    source=p.add_mutually_exclusive_group(required=True)
    source.add_argument('--csv',help='Daily OHLC CSV, including prior warmup history')
    source.add_argument('--database',action='store_true')
    p.add_argument('--from-date',default='2020-01-01')
    p.add_argument('--to-date',default='2026-10-23')
    p.add_argument('--evaluation-start',default='2021-01-01')
    p.add_argument('--fast',type=int,default=20)
    p.add_argument('--slow',type=int,default=50)
    p.add_argument('--puts-only',action='store_true')
    p.add_argument('--skip-incomplete-days',action='store_true')
    p.add_argument('--output-dir',default='outputs/nifty_daily_ema_switch')
    args=p.parse_args()
    try:
        excluded=[]
        if args.database:
            from backtest.snapshot_cache import load_snapshot_candles
            minutes=load_snapshot_candles(date.fromisoformat(args.from_date),date.fromisoformat(args.to_date),
                database_only=True,skip_incomplete_days=args.skip_incomplete_days,excluded_days=excluded)
            rows=daily_from_minutes(minutes)
            del minutes
        else:
            if args.skip_incomplete_days:
                p.error('--skip-incomplete-days requires --database')
            with open(args.csv,newline='') as f:
                rows=list(csv.DictReader(f))
        report,trades,signals,curve=run(rows,evaluation_start=args.evaluation_start,fast=args.fast,slow=args.slow,puts_only=args.puts_only)
        report['excluded_incomplete_days']=list(map(str,excluded)) if args.database else None
        report['data_source']='market_snapshots NIFTY 50 / 1m, aggregated daily' if args.database else args.csv
        folder=Path(args.output_dir)
        folder.mkdir(parents=True,exist_ok=True)
        (folder/'summary.json').write_text(json.dumps(report,indent=2,default=str,allow_nan=False))
        for name,values in (('trades',trades),('signals',signals),('equity_points',curve)):
            save_csv(folder/f'{name}.csv',values)
        print(json.dumps(report,indent=2,default=str,allow_nan=False))
        print('Results:',folder)
    except (ValueError,KeyError,OSError) as exc:
        p.error(str(exc))


if __name__=='__main__':
    main()
