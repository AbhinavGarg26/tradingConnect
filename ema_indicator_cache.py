"""Build/rebuild deterministic EMA cache tables. No broker or execution adapter."""
import argparse
from datetime import datetime
import json
from zoneinfo import ZoneInfo


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--strategy',choices=('daily_calls','bearish_puts','bullish_calls','all'),default='all')
    parser.add_argument('--rebuild',action='store_true',help='Explicitly recompute indicator cache after reviewing historical source corrections; never changes trades/checkpoints')
    args=parser.parse_args()
    from nifty_15m_env import load_nifty_environment
    load_nifty_environment()
    from trading.database import get_db,engine
    from trading.ema_feed import Calendar
    from trading.ema_indicator_cache import IndicatorCache
    from sqlalchemy import inspect
    schema=inspect(engine)
    if any(not schema.has_table(t) for t in ('strategy_indicator_caches','strategy_indicator_candles')):
        raise RuntimeError('Apply Rails migration 20261024060000 before building EMA caches')
    now=datetime.now(ZoneInfo('Asia/Kolkata'));calendar=Calendar(get_db)
    output={}
    for kind in (('daily_calls','bearish_puts','bullish_calls') if args.strategy=='all' else (args.strategy,)):
        cache=IndicatorCache(get_db,kind,calendar,skip_incomplete_days=True)
        cache.sync_history(now,rebuild=args.rebuild)
        output[kind]=dict(cache_key=cache.key,samples=cache.data['state']['count'],source_through=str(cache.data['source_through']),
            last_candle=str(cache.data['last_candle_time']),excluded_days=cache.data['excluded_days'])
    print(json.dumps(dict(indicator_cache_only=True,orders_enabled=False,caches=output),indent=2))

if __name__=='__main__':main()
