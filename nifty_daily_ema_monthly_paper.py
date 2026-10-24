"""One-shot, paper-only monthly Nifty 20/50 tracker. Cannot submit broker orders."""
import argparse
import json
import logging
from dataclasses import asdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from strategies.nifty_daily_ema_monthly import KEY, VERSION, Settings, action, quote_fill, select_monthly_option, trend

IST = ZoneInfo('Asia/Kolkata')
log = logging.getLogger(__name__)


def paper_step(db, kite, user_id, rows, instruments, today, now, settings, is_trading_day, *, check=False):
    from sqlalchemy import select, text, func
    from trading.strategy_records import StrategyRun, StrategyTrade, StrategyEvent
    state = trend(rows)
    # An account/strategy advisory lock spans lookup, quotes and journal writes.
    if not check:
        db.execute(text('SELECT pg_advisory_xact_lock(hashtext(:key))'), {'key': f'{user_id}:{KEY}:paper'})
        nullable = dict(db.execute(text("SELECT column_name,is_nullable FROM information_schema.columns WHERE table_schema='public' AND table_name='strategy_trades' AND column_name IN ('initial_stop','current_stop','target')")).all())
        if any(nullable.get(k) != 'YES' for k in ('initial_stop','current_stop','target')):
            raise RuntimeError('Apply the positional-strategy nullable-stop migration before journaling')
    positions = list(db.scalars(select(StrategyTrade).where(StrategyTrade.user_id == user_id,
        StrategyTrade.strategy_key == KEY, StrategyTrade.status.notin_(['closed', 'rejected']))))
    if len(positions) > 1 or any(t.mode != 'paper' or t.status != 'open' for t in positions):
        raise RuntimeError('Unresolved or non-paper strategy position; manual reconciliation required')
    position = positions[0] if positions else None
    decision = action(state, {'expiry': position.option_expiry, 'side': position.side} if position else None,
                      today, settings, is_trading_day)
    side = 'CE' if state['bullish'] else None
    summary = dict(strategy=KEY, version=VERSION, mode='paper', side=side, signal=decision,
                   trend='bullish' if state['bullish'] else 'bearish' if state['fast'] < state['slow'] else 'neutral',
                   completed_date=str(state['date']), fast_ema=state['fast'], slow_ema=state['slow'],
                   premium_cap=str(settings.max_premium), lot_count=1, real_orders_enabled=False)
    if check:
        return summary
    prior = db.scalar(select(StrategyRun).where(StrategyRun.user_id == user_id,
        StrategyRun.strategy_key == KEY, StrategyRun.mode == 'paper',
        StrategyRun.configuration['session_date'].astext == str(today)))
    if prior:
        return dict(summary, status='already_processed', run_id=prior.id)
    run = StrategyRun(user_id=user_id, strategy_key=KEY, strategy_version=VERSION, mode='paper',
        status='running', started_at=now, heartbeat_at=now,
        configuration=dict(session_date=str(today), fast=20, slow=50, timeframe='1d',
            max_premium=str(settings.max_premium), lot_count=1, product='NRML', two_sided=False,
            expiry='monthly_from_listed_futures', roll_sessions=settings.roll_sessions,
            actual_option_quotes=True, activation='paper_only'))
    db.add(run)
    db.flush()

    def quote(symbol, buying, quantity):
        q = dict(kite.quote([f'NFO:{symbol}'])[f'NFO:{symbol}'])
        stamp = q.get('timestamp')
        if isinstance(stamp, datetime) and stamp.tzinfo is None:
            q['timestamp'] = stamp.replace(tzinfo=IST)
        return quote_fill(q, buying=buying, quantity=quantity, now=now, settings=settings)

    contract = entry_price = exit_price = None
    if decision in {'enter', 'roll'}:
        spot_quote = kite.quote(['NSE:NIFTY 50'])['NSE:NIFTY 50']
        stamp = spot_quote.get('timestamp')
        if isinstance(stamp, datetime) and stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=IST)
        if stamp is None or not 0 <= (now-stamp).total_seconds() <= 60:
            raise ValueError('Fresh spot quote required')
        spot = Decimal(str(spot_quote['last_price']))
        contract = select_monthly_option(instruments, today, spot, settings, is_trading_day, side)
        quantity = int(contract['lot_size'])
        entry_price = quote(contract['tradingsymbol'], True, quantity)
    else:
        spot = Decimal(str(state['close']))
    if position and decision in {'exit', 'roll'}:
        exit_price = quote(position.option_symbol, False, position.quantity)
        if contract is None:
            spot_quote = kite.quote(['NSE:NIFTY 50'])['NSE:NIFTY 50']
            stamp = spot_quote.get('timestamp')
            if isinstance(stamp, datetime) and stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=IST)
            if stamp is None or not 0 <= (now-stamp).total_seconds() <= 60:
                raise ValueError('Fresh exit spot quote required')
            spot = Decimal(str(spot_quote['last_price']))
        position.exit_signal_time = position.exit_confirmed_at = now
        position.exit_reason = 'monthly_roll' if decision == 'roll' else 'daily_ema_reversal'
        position.exit_premium = exit_price
        position.exit_index = spot
        position.index_points = (spot-position.entry_index)*(1 if position.side == 'CE' else -1)
        position.gross_option_pnl = (exit_price-position.entry_premium)*position.quantity
        position.status = 'closed'
        position.updated_at = now
        db.add(StrategyEvent(strategy_run_id=run.id, strategy_trade_id=position.id, occurred_at=now,
            action='PAPER_EXIT_'+position.side, reason=position.exit_reason,
            index_price=spot, details={'premium': str(exit_price), 'quote_side': 'bid', 'real_order': False}))
    if contract:
        trade = StrategyTrade(strategy_run_id=run.id, user_id=user_id, strategy_key=KEY,
            strategy_version=VERSION, mode='paper', trading_date=today, trade_number=1, status='open', side=side,
            option_symbol=contract['tradingsymbol'], option_expiry=contract['expiry'], option_strike=contract['strike'],
            lot_count=1, quantity=quantity, entry_signal_time=datetime.combine(state['date'],time(15,30),IST),
            entry_confirmed_at=now, entry_reason='monthly_roll' if decision == 'roll' else 'daily_ema_crossover',
            entry_index=spot, entry_premium=entry_price, initial_stop=None, current_stop=None, target=None)
        db.add(trade)
        db.flush()
        db.add(StrategyEvent(strategy_run_id=run.id, strategy_trade_id=trade.id, occurred_at=now,
            action='PAPER_BUY_'+side, reason=trade.entry_reason, index_price=spot,
            details={'premium': str(entry_price), 'quantity': quantity, 'quote_side': 'ask', 'real_order': False}))
        summary['option_symbol'] = contract['tradingsymbol']
    db.add(StrategyEvent(strategy_run_id=run.id, occurred_at=now, action='DAILY_EMA_SIGNAL',
        reason=decision, index_price=Decimal(str(state['close'])), details=summary))
    run.status = 'completed'
    run.ended_at = run.heartbeat_at = run.updated_at = now
    return dict(summary, status='paper_recorded', run_id=run.id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--max-premium', type=Decimal, default=Decimal('30000'))
    parser.add_argument('--roll-sessions', type=int, default=2)
    parser.add_argument('--check', action='store_true', help='Read-only signal/position readiness check')
    parser.add_argument('--skip-incomplete-days', action='store_true')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    settings = Settings(args.max_premium, args.roll_sessions)
    now = datetime.now(IST)
    if not args.check and not time(9,16) <= now.time() < time(9,30):
        parser.error('Paper fills permitted only 09:16–09:30 IST; use --check for a read-only signal')
    from nifty_15m_env import load_nifty_environment
    load_nifty_environment()
    from trading.database import get_db
    from trading.user_token import fetch_user_token
    from backtest.snapshot_cache import load_snapshot_candles
    from backtest.nifty_daily_ema import daily_from_minutes
    from market.trading_calendar import is_nse_trading_day
    from backtest.session_calendar import regular_session_closed
    excluded = []
    prior_day = now.date()-timedelta(days=1)
    minutes = load_snapshot_candles(date(2020,1,1),prior_day,database_only=True,
        skip_incomplete_days=args.skip_incomplete_days,excluded_days=excluded)
    rows = daily_from_minutes(minutes)
    del minutes
    kite, user_id = fetch_user_token(log)
    kite.profile()
    instruments = [] if args.check else kite.instruments('NFO')
    now = datetime.now(IST)
    if not args.check and not time(9,16) <= now.time() < time(9,30):
        parser.error('Opening execution window ended while loading data; no paper fill recorded')
    with get_db() as db:
        def trading_day(day):
            return not regular_session_closed(day) and is_nse_trading_day(day, db)
        if not args.check and not trading_day(now.date()):
            parser.error('No regular trading session today')
        result = paper_step(db,kite,user_id,rows,instruments,now.date(),now,settings,trading_day,check=args.check)
    result['excluded_incomplete_days'] = list(map(str,excluded))
    print(json.dumps(result,indent=2,default=str))


if __name__ == '__main__':
    main()
