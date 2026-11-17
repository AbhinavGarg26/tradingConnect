from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from nifty_15m_env import load_nifty_environment
load_nifty_environment()
from invalidate_profit_lock_paper import invalidate


def records(mode='paper'):
    run = SimpleNamespace(id=79, user_id=1, strategy_key='one_direction_profit_lock',
                          mode=mode, status='failed', ended_at=None)
    trade = SimpleNamespace(id=10, user_id=1, strategy_key=run.strategy_key, mode=mode,
        trading_date=date(2026,11,17), option_symbol='NIFTY26N1725450CE', status='reconcile',
        entry_order_id=None, exit_order_id=None, protection_order_id=None,
        exit_premium=None, exit_confirmed_at=None, exit_reason='stop',
        exit_signal_time='old', exit_index=25438, index_points=0, gross_option_pnl=None,
        error_message='DetachedInstanceError')
    db = Mock()
    db.get.return_value = run
    db.scalars.return_value = [trade]
    return db, run, trade


def test_preview_does_not_modify_trade():
    db, run, trade = records()
    invalidate(db, 79, trade.trading_date, trade.option_symbol)
    assert trade.status == 'reconcile'
    db.add.assert_not_called()


def test_apply_preserves_audit_without_fake_pnl():
    db, run, trade = records()
    invalidate(db, 79, trade.trading_date, trade.option_symbol, apply=True)
    assert trade.status == 'invalidated'
    assert trade.gross_option_pnl is None and trade.exit_premium is None
    assert trade.exit_signal_time is None and trade.index_points is None
    assert db.add.call_args.args[0].details['original']['exit_reason'] == 'stop'


@pytest.mark.parametrize('change', ['live', 'broker', 'wrong_symbol', 'running'])
def test_refuses_unsafe_or_mismatched_records(change):
    db, run, trade = records()
    symbol = trade.option_symbol
    if change == 'live': run.mode = 'live'
    if change == 'broker': trade.entry_order_id = 'broker-id'
    if change == 'wrong_symbol': symbol = 'OTHER'
    if change == 'running': run.status = 'running'
    with pytest.raises(ValueError):
        invalidate(db, 79, trade.trading_date, symbol, apply=True)
    assert trade.status == 'reconcile'
    db.add.assert_not_called()
