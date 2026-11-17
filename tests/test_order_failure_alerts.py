from unittest.mock import Mock, patch

import pytest

from nifty_15m_env import load_nifty_environment
load_nifty_environment()

from trading.order_failure_alerts import instrument_kite


def test_kite_failure_alert_preserves_exception_and_does_not_retry():
    error = RuntimeError('Insufficient funds')
    kite = Mock(_post=Mock(side_effect=error))
    kite._order_failure_alerts_installed = False
    instrument_kite(kite, 123)
    instrument_kite(kite, 123)
    params = dict(order_type='MARKET', tradingsymbol='NIFTY', quantity=65)
    with patch('trading.order_failure_alerts.notify_order_failure') as alert:
        with pytest.raises(RuntimeError) as result:
            kite._post('order.place', params=params)
        assert result.value is error
        alert.assert_called_once_with(123, 'Kite', params, error)
    assert kite._post.__wrapped__.call_count == 1


def test_limit_failure_does_not_send_market_order_alert():
    kite = Mock(_post=Mock(side_effect=RuntimeError('rejected')))
    kite._order_failure_alerts_installed = False
    instrument_kite(kite, 123)
    with patch('trading.order_failure_alerts.notify_order_failure') as alert:
        with pytest.raises(RuntimeError):
            kite._post('order.place', params={'order_type': 'LIMIT'})
        alert.assert_not_called()


def test_groww_market_failure_preserves_exception():
    from market.recovery_gtt_executor import GrowwRecoveryGttExecutor
    client = GrowwRecoveryGttExecutor('token', Mock(), 123)
    error = RuntimeError('Insufficient funds')
    params = {'order_type': 'MARKET', 'trading_symbol': 'NIFTY'}
    with patch.object(client, '_request_impl', side_effect=error) as request, patch(
        'trading.order_failure_alerts.notify_order_failure'
    ) as alert:
        with pytest.raises(RuntimeError) as result:
            client._request('POST', '/order/create', params)
        assert result.value is error
        request.assert_called_once()
        alert.assert_called_once_with(123, 'Groww', params, error)


def test_profit_lock_exit_does_not_read_trade_after_session_closes():
    from contextlib import contextmanager
    from decimal import Decimal
    from types import SimpleNamespace
    from trading.one_direction_profit_lock_journal import ProfitLockJournal

    class Trade:
        status = 'open'
        entry_index = Decimal('25000')
        detached = False

        @property
        def side(self):
            if self.detached:
                raise RuntimeError('Detached trade attribute access')
            return 'CE'

    trade = Trade()

    @contextmanager
    def factory():
        yield SimpleNamespace(get=lambda *args, **kwargs: trade)
        trade.detached = True

    journal = object.__new__(ProfitLockJournal)
    journal.factory, journal.trade_id = factory, 1
    journal.event = Mock()
    snapshot = SimpleNamespace(observed_at='signal-time', close=Decimal('25010'))
    journal.exit_intent('stop', snapshot)
    journal.event.assert_called_once_with('EXIT_CE', 'stop', occurred_at='signal-time')
    assert trade.status == 'exit_pending'


def test_cooldown_claim_suppresses_repeated_alerts():
    from contextlib import contextmanager
    from trading.order_failure_alerts import notify_order_failure

    db = Mock()
    db.execute.return_value.first.side_effect = [('123',), None]

    @contextmanager
    def factory():
        yield db

    with patch('trading.database.get_db', factory), patch('trading.alerts.Alerter.from_db') as alerter:
        for _ in range(2):
            notify_order_failure(123, 'Kite', {'order_type': 'MARKET'}, RuntimeError('rejected'))
        alerter.return_value.send.assert_called_once()
        sql = str(db.execute.call_args.args[0])
        assert "INTERVAL '5 minutes'" in sql
        assert 'ON CONFLICT' in sql
