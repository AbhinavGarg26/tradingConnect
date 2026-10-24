"""Compare v2 baseline and v3 30m-EMA opening-range re-entry variants."""
import argparse
import csv
import json
from datetime import time, timedelta
from decimal import Decimal
from pathlib import Path

from backtest.fixed_profit import aggregate, load_sessions, run_session, statistics
from backtest.fixed_profit_diagnostics import CONFIG, exit_trade, periods
from strategies.fixed_profit_ema import FixedProfitEMA


class ClosedEMA:
    """SMA-seeded recursive EMA carried across sessions; no overnight fake bars."""
    def __init__(self,period):
        if period<2:raise ValueError('EMA period must exceed 1')
        self.period=period;self.count=0;self.seed=[]
        self.value=self.previous=self.closed_at=None

    def update(self,close,closed_at):
        close=Decimal(str(close))
        if not close.is_finite() or close<=0 or closed_at.tzinfo is None:
            raise ValueError('Positive finite close and aware timestamp required')
        if self.closed_at and closed_at<=self.closed_at:
            raise ValueError('EMA closes must be unique and chronological')
        self.closed_at=closed_at;self.count+=1
        if self.value is None:
            self.seed.append(close)
            if len(self.seed)==self.period:
                self.value=sum(self.seed)/self.period;self.previous=self.value
        else:
            self.previous=self.value
            self.value+=(close-self.value)*Decimal(2)/Decimal(self.period+1)


def trend_schedule(sessions):
    emas={p:ClosedEMA(p) for p in (30,35,36)}
    schedule={};eligible_days=set()
    for day,bars in sessions:
        for i in range(29,360,30):
            closed_at=bars[i].time+timedelta(minutes=1)
            for ema in emas.values():ema.update(bars[i].close,closed_at)
            # Shared 108 completed candles, independent of which period wins.
            if emas[36].count>=108:
                eligible_days.add(day)
                schedule[closed_at]={p:(e.value,e.previous,e.closed_at) for p,e in emas.items()}
    return schedule,eligible_days


def run_variant(bars,schedule,period=None,slope=False):
    engine=FixedProfitEMA(require_ema=period is not None,require_slope=slope)
    engine.set_reference(aggregate(bars[:30]),observed_at=bars[30].time)
    if not engine.eligible:return None
    last_trend=None
    # At 09:45 the current opening 30m EMA becomes available.
    if bars[30].time in schedule:
        last_trend=schedule[bars[30].time]
    for i in range(30,len(bars)):
        bar=bars[i]
        if bar.time in schedule:last_trend=schedule[bar.time]
        if period is not None and last_trend:
            value,previous,closed_at=last_trend[period]
            engine.set_trend(value,previous,closed_at=closed_at)
        if engine.pending:
            try:engine.confirm_entry(index_price=bar.open)
            except ValueError:engine.reject_signal()
            else:
                direction=1 if engine.position=='CE' else -1
                points,end,reason,ambiguous=exit_trade(bars,i,direction)
                return dict(date=str(bar.time.date()),side=engine.position,
                            entry_time=bar.time.isoformat(),entry=float(bar.open),
                            stop=float(engine.stop),target=float(engine.target),
                            reference_high=float(engine.reference.high),reference_low=float(engine.reference.low),
                            ema=float(engine.trend[0]) if engine.trend else None,
                            ema_closed_at=engine.trend[2].isoformat() if engine.trend else None,
                            exit_bar_time=bars[end].time.isoformat(),points=points,exit_reason=reason,
                            ambiguous_exit=ambiguous)
        if (i-29)%5==0:
            observed_at=bar.time+timedelta(minutes=1)
            if observed_at in schedule:
                last_trend=schedule[observed_at]
                if period is not None:
                    value,previous,closed_at=last_trend[period]
                    engine.set_trend(value,previous,closed_at=closed_at)
            engine.on_bar(aggregate(bars[i-4:i+1]),observed_at=observed_at)
    return None


def analyze(path,output):
    sessions,excluded=load_sessions(path)
    schedule,eligible_days=trend_schedule(sessions)
    # Require EMA warmup already available at opening reference close, for every variant.
    shared=[(d,b) for d,b in sessions if b[30].time in schedule]
    variants=[('baseline',None,False),('reentry_no_ema',None,False),
              ('reentry_ema30',30,False),('reentry_ema35',35,False),
              ('reentry_ema36',36,False),('reentry_ema36_slope',36,True)]
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    comparisons={}
    for name,period,slope in variants:
        trades=[]
        for day,bars in shared:
            trade=run_session(bars,CONFIG) if name=='baseline' else run_variant(bars,schedule,period,slope)
            if trade:trades.append(trade)
        comparisons[name]=dict(period=period,require_slope=slope,metrics=periods(trades),
                               yearly={str(y):statistics([t for t in trades if t['date'].startswith(str(y))])
                                       for y in range(2020,2027)},
                               by_side={s:statistics([t for t in trades if t['side']==s]) for s in ('CE','PE')})
        with (output/f'{name}_trades.csv').open('w') as f:
            if trades:
                writer=csv.DictWriter(f,fieldnames=list(trades[0]));writer.writeheader();writer.writerows(trades)
        print(name,comparisons[name]['metrics']['all'],flush=True)
    eligible=[name for name,_,_ in variants if 'ema' in name and name!='reentry_no_ema' and
              comparisons[name]['metrics']['train']['gross']['trades']>=40]
    selected=max(eligible,key=lambda n:comparisons[n]['metrics']['train']['cost_2']['avg_points']) if eligible else None
    report=dict(complete_sessions=len(sessions),shared_sessions=len(shared),
                first_date=str(shared[0][0]) if shared else None,last_date=str(shared[-1][0]) if shared else None,
                excluded_dates=excluded,warmup='108 completed 30m bars; common evaluation dates; SMA seed; carries across sessions',
                selected_on_training=selected,comparisons=comparisons)
    (output/'analysis.json').write_text(json.dumps(report,indent=2))
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv_path');parser.add_argument('--output',default='outputs/fixed-profit-ema')
    args=parser.parse_args();analyze(args.csv_path,args.output)
