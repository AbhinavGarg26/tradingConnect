from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from market.portfolio_summary import dispatch_summary, groww_totals, report_message

NOW = datetime(2026, 11, 17, 10, 0, tzinfo=ZoneInfo('Asia/Kolkata'))
FUNDS = dict(current_pnl=1000, realised_pnl=600, unrealised_pnl=400, total_funds=10000, available_balance=8000)
GROWW = dict(current_pnl=-200, realised_pnl=-300, unrealised_pnl=100)
ALERTER = SimpleNamespace(_enabled=True, bot_token='test', chat_id='test', TELEGRAM_URL='https://test.invalid/{token}')


@pytest.fixture
def factory():
    engine = create_engine('sqlite://')
    with engine.begin() as conn:
        conn.execute(text('''CREATE TABLE portfolio_summary_deliveries (
            id INTEGER PRIMARY KEY, user_id BIGINT, session_date DATE, checkpoint TEXT,
            status TEXT, created_at TIMESTAMP, updated_at TIMESTAMP,
            UNIQUE(user_id, session_date, checkpoint))'''))
    @contextmanager
    def session():
        with Session(engine) as db:
            with db.begin():
                yield db
    yield session
    engine.dispose()


def test_report_separates_accounts_and_combines_correctly():
    message = report_message(FUNDS, GROWW, '10:00', NOW)
    assert 'Kite' in message and 'Groww' in message
    assert 'Combined P&amp;L: ₹800.00' in message
    assert 'Realised ₹300.00 | Open ₹500.00' in message


def test_missing_groww_is_not_zero():
    message = report_message(FUNDS, None, '10:00', NOW)
    assert 'Combined P&amp;L</b>: unavailable' in message
    assert 'Combined P&amp;L: ₹1,000' not in message


def test_restarts_do_not_replay_and_checkpoints_and_users_are_independent(factory):
    response = Mock(status_code=200, json=lambda: {'ok': True})
    with patch('market.portfolio_summary.httpx.post', return_value=response) as send:
        for user, checkpoint in [(7, '10:00'), (7, '10:00'), (7, '11:00'), (8, '10:00')]:
            dispatch_summary(factory, user, checkpoint, NOW, FUNDS, ALERTER, lambda *_: GROWW)
        assert send.call_count == 3
    with factory() as db:
        assert db.execute(text("SELECT COUNT(*) FROM portfolio_summary_deliveries WHERE status='sent'")).scalar() == 3


def test_timeout_remains_uncertain_and_is_never_automatically_retried(factory):
    with patch('market.portfolio_summary.httpx.post', side_effect=TimeoutError) as send:
        dispatch_summary(factory, 7, '10:00', NOW, FUNDS, ALERTER, lambda *_: GROWW)
        dispatch_summary(factory, 7, '10:00', NOW, FUNDS, ALERTER, lambda *_: GROWW)
        assert send.call_count == 1
    with factory() as db:
        assert db.execute(text('SELECT status FROM portfolio_summary_deliveries')).scalar() == 'uncertain'


def test_telegram_api_rejection_is_not_success(factory):
    with patch('market.portfolio_summary.httpx.post', return_value=Mock(status_code=200, json=lambda: {'ok': False})):
        assert not dispatch_summary(factory, 7, '10:00', NOW, FUNDS, ALERTER, lambda *_: GROWW)
    with factory() as db:
        assert db.execute(text('SELECT status FROM portfolio_summary_deliveries')).scalar() == 'failed'


def test_partial_broker_failure_still_delivers_labelled_kite_report(factory):
    def unavailable(*_):
        raise ValueError('invalid broker response')
    with patch('market.portfolio_summary.httpx.post', return_value=Mock(status_code=200, json=lambda: {'ok': True})) as send:
        assert dispatch_summary(factory, 7, '10:00', NOW, FUNDS, ALERTER, unavailable)
        assert 'unavailable (Groww incomplete)' in send.call_args.kwargs['json']['text']


def test_groww_marks_unreconciled_entry_or_missing_quotes_incomplete():
    row = dict(trading_symbol='ABC', exchange='NSE', segment='CASH', product='CNC', quantity=10, realised_pnl=30)
    assert groww_totals([row], {('NSE', 'CASH', 'ABC'): 110}, {}) is None
    order = dict(trading_symbol='ABC', exchange='NSE', product='CNC', filled_quantity=10, average_fill_price=100, transaction_type='BUY')
    assert groww_totals([row], {}, {'CASH': [order]}) is None
    result = groww_totals([row], {('NSE', 'CASH', 'ABC'): 110}, {'CASH': [order]})
    assert result == dict(realised_pnl=30, unrealised_pnl=100, current_pnl=130)


def test_disabled_telegram_does_not_consume_delivery_claim(factory):
    disabled = SimpleNamespace(_enabled=False)
    assert not dispatch_summary(factory, 7, '10:00', NOW, FUNDS, disabled)
    with factory() as db:
        assert db.execute(text('SELECT COUNT(*) FROM portfolio_summary_deliveries')).scalar() == 0


def test_crashed_sending_claim_is_not_replayed(factory):
    with factory() as db:
        db.execute(text("INSERT INTO portfolio_summary_deliveries (user_id, session_date, checkpoint, status) VALUES (7, :day, '10:00', 'sending')"), {'day': NOW.date()})
    with patch('market.portfolio_summary.httpx.post') as send:
        assert not dispatch_summary(factory, 7, '10:00', NOW, FUNDS, ALERTER)
        send.assert_not_called()


def test_groww_snapshot_fetches_both_segments_and_reconciles_fills():
    from market.portfolio_summary import groww_snapshot
    row = dict(trading_symbol='ABC', exchange='NSE', product='CNC', quantity=10, realised_pnl=30)
    order = dict(trading_symbol='ABC', exchange='NSE', product='CNC', filled_quantity=10, average_fill_price=100, transaction_type='BUY')
    client = Mock()
    def read(path, params):
        assert path in ('/positions/user', '/order/list')
        payload = {'positions': [row] if params['segment'] == 'CASH' else []} if path == '/positions/user' else {'order_list': [order]}
        return Mock(json=lambda: {'status': 'SUCCESS', 'payload': payload})
    client.get.side_effect = read
    link = SimpleNamespace(is_session_valid=True, decrypt_session_token=lambda db: 'test')
    with patch('trading.exchange_link.ExchangeLinkRepo.get_for_user', return_value=link), patch('market.portfolio_summary.httpx.Client') as ctor, patch('market.portfolio_summary.live_prices', return_value={('NSE', 'CASH', 'ABC'): 110}):
        ctor.return_value.__enter__.return_value = client
        result = groww_snapshot(Mock(), 7, Mock())
    assert result['current_pnl'] == 130
    assert {call.kwargs['params']['segment'] for call in client.get.call_args_list} == {'CASH', 'FNO'}


def test_non_finite_or_missing_groww_realised_is_incomplete():
    assert groww_totals([dict(realised_pnl=float('nan'), quantity=0)], {}, {}) is None
    assert groww_totals([dict(quantity=0)], {}, {}) is None
    assert groww_totals([], {}, {})['current_pnl'] == 0
