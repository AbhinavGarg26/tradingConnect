"""One-hour bearish EMA comparison, using the verified 30m database export."""
import argparse
import csv
import json
from datetime import timedelta
from pathlib import Path
from backtest.snapshot_cache import exchange_time, valid_ohlc
from backtest.nifty_30m_put_comparison import compare


def aggregate_hourly(rows):
    days={}
    previous=None
    for row in rows:
        stamp=exchange_time(row['date'])
        if previous is not None and stamp<=previous:
            raise ValueError('Source candles must be unique and chronological')
        previous=stamp
        if not valid_ohlc(row):
            raise ValueError('Invalid source OHLC')
        days.setdefault(stamp.date(),[]).append((stamp,row))
    hourly=[]
    for day,group in days.items():
        opening=group[0][0].replace(hour=9,minute=15,second=0,microsecond=0)
        if len(group)!=13 or any(stamp!=opening+timedelta(minutes=30*i)
            or int(row['duration_minutes'])!=(15 if i==12 else 30) for i,(stamp,row) in enumerate(group)):
            raise ValueError(f'{day}: require twelve complete 30m bars plus final 15m bar')
        for offset in range(0,13,2):
            bucket=group[offset:offset+2]
            values=[row for _,row in bucket]
            hourly.append(dict(date=bucket[0][0],duration_minutes=sum(int(r['duration_minutes']) for r in values),
                               open=float(values[0]['open']),close=float(values[-1]['close']),
                               high=max(float(r['high']) for r in values),low=min(float(r['low']) for r in values)))
    return hourly


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--csv',default='outputs/nifty_30m_put_comparison/candles_30m.csv')
    p.add_argument('--source-report',default='outputs/nifty_30m_put_comparison/comparison.json')
    p.add_argument('--evaluation-start',default='2021-01-01')
    p.add_argument('--output-dir',default='outputs/nifty_1h_put_comparison')
    args=p.parse_args()
    with open(args.csv,newline='') as f: rows=list(csv.DictReader(f))
    source=json.loads(Path(args.source_report).read_text())
    bars=aggregate_hourly(rows)
    compare(bars,args.output_dir,evaluation_start=args.evaluation_start,
            excluded=source['excluded_incomplete_days'],timeframe_minutes=60)
    path=Path(args.output_dir)/'comparison.json'
    report=json.loads(path.read_text())
    report['source_candles']=args.csv
    report['source_report']=args.source_report
    report['aggregation']='Exact OHLC aggregation of consecutive verified 30m candles within each session; no cross-session aggregation'
    path.write_text(json.dumps(report,indent=2)+'\n')


if __name__=='__main__':
    main()
