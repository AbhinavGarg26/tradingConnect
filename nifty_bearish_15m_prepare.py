"""Read-only readiness for frozen 15m 9/21 puts. No live or paper order submission."""
import argparse
import csv
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from strategies.nifty_bearish_15m import ExecutionProfile,readiness
from backtest.nifty_bearish_ema_timeframes import verify_baseline


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--csv',default='outputs/nifty_15m_put_comparison/candles_15m.csv')
    p.add_argument('--output',default='outputs/nifty_bearish_15m_preparation/readiness.json')
    args=p.parse_args()
    verify_baseline()
    with open(args.csv,newline='') as f:
        rows=list(csv.DictReader(f))
    result=readiness(rows,datetime.now(ZoneInfo('Asia/Kolkata')))
    result['execution_profile']=asdict(ExecutionProfile())
    result['data_source']=args.csv
    result['readiness_status']='signal_engine_verified; option execution and deployment not activated'
    result['outstanding']=['Historical option-premium validation including monthly rolls and costs',
                           'Broker-specific order/reconciliation adapter review',
                           'Option position protection and overnight risk policy',
                           'No service/timer enabled; user-controlled deployment and activation required']
    target=Path(args.output)
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(result,indent=2,default=str,allow_nan=False))
    print(json.dumps(result,indent=2,default=str,allow_nan=False))


if __name__=='__main__':
    main()
