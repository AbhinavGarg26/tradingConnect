"""Invalidate an interrupted paper sample without inventing an exit fill.

Defaults to preview. Stop the strategy service before applying this repair.
No broker APIs are called. Original fields are preserved in an audit event.
"""
import argparse
from datetime import date, datetime, timezone


def invalidate(db, run_id, trading_date, symbol, *, apply=False):
    from sqlalchemy import select
    from trading.strategy_records import StrategyEvent, StrategyRun, StrategyTrade

    run = db.get(StrategyRun, run_id, with_for_update=True)
    if run is None or run.strategy_key != 'one_direction_profit_lock' or run.mode != 'paper':
        raise ValueError('Expected an existing One Direction Profit Lock PAPER run')
    if run.status == 'running':
        raise ValueError('Run is still running; stop the service and review its state first')
    rows = list(db.scalars(select(StrategyTrade).where(
        StrategyTrade.strategy_run_id == run_id,
        StrategyTrade.status.in_(['open', 'entry_pending', 'exit_pending', 'reconcile']),
    ).with_for_update()))
    if len(rows) != 1:
        raise ValueError(f'Expected exactly one unresolved trade; found {len(rows)} (no changes)')
    trade = rows[0]
    if (trade.mode != 'paper' or trade.user_id != run.user_id
            or trade.strategy_key != run.strategy_key
            or trade.trading_date != trading_date or trade.option_symbol != symbol):
        raise ValueError('Trade identity does not match the requested paper sample')
    if any((trade.entry_order_id, trade.exit_order_id, trade.protection_order_id)):
        raise ValueError('Broker order IDs are present; manual review required')
    if trade.exit_premium is not None or trade.exit_confirmed_at is not None:
        raise ValueError('An exit fill is present; review before invalidating')
    print(f'Run {run.id}, trade {trade.id}, user {trade.user_id}, {trade.option_symbol}: {trade.status}')
    if not apply:
        print('PREVIEW: would invalidate this paper sample; no changes made')
        return
    now = datetime.now(timezone.utc)
    reason = 'paper_exit_interrupted_no_fill'
    original = {name: str(getattr(trade, name)) if getattr(trade, name) is not None else None
                for name in ('status', 'exit_reason', 'exit_signal_time', 'exit_index',
                             'index_points', 'gross_option_pnl', 'error_message')}
    db.add(StrategyEvent(
        strategy_run_id=run.id, strategy_trade_id=trade.id, occurred_at=now,
        action='PAPER_INVALIDATED', reason=reason,
        details={'original': original, 'note': 'Manual repair; exit quote unavailable; no P&L assigned'},
    ))
    trade.status = 'invalidated'
    trade.exit_reason = reason
    trade.exit_signal_time = trade.exit_index = trade.index_points = None
    trade.gross_option_pnl = None
    trade.error_message = 'Paper sample invalidated after interrupted exit; no exit fill or P&L available'
    trade.updated_at = now
    run.status = 'failed'
    run.ended_at = run.ended_at or now
    run.updated_at = now
    print('Paper sample invalidated; original data retained in PAPER_INVALIDATED event; P&L unknown')


def main():
    from nifty_15m_env import load_nifty_environment
    load_nifty_environment()
    from trading.database import get_db
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', type=int, required=True)
    parser.add_argument('--date', type=date.fromisoformat, required=True)
    parser.add_argument('--symbol', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    with get_db() as db:
        invalidate(db, args.run_id, args.date, args.symbol, apply=args.apply)


if __name__ == '__main__':
    main()
