from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from nifty_15m_env import load_nifty_environment
load_nifty_environment()
from trading import strategy_risk


@pytest.mark.parametrize('naive,age,healthy,expected', [
    (True, 5, True, True), (False, 5, True, True),
    (True, 60, True, False), (False, 60, True, False),
    (True, -60, True, False), (True, 5, False, False),
])
def test_guardian_heartbeat_handles_utc_timestamps(monkeypatch, naive, age, healthy, expected):
    heartbeat = datetime.now(timezone.utc) - timedelta(seconds=age)
    if naive: heartbeat = heartbeat.replace(tzinfo=None)
    status = SimpleNamespace(status='healthy' if healthy else 'error', heartbeat_at=heartbeat)

    @contextmanager
    def factory():
        yield SimpleNamespace(scalar=lambda query: status)

    monkeypatch.setattr(strategy_risk, 'get_db', factory)
    assert strategy_risk.guardian_fresh(1, 'test') is expected


def test_insufficient_cash_skips_pending_entry_without_submission():
    from trading.ema_execution import Executor, EntryBlocked
    pending = {'action':'entry', 'time':datetime.now(timezone.utc).isoformat(), 'side':'CE'}
    store = Mock(user_id=1, key='test', mode='live')
    store.state.return_value = {'pending_signal':pending}
    store.intent.return_value = None
    executor = object.__new__(Executor)
    executor.store = store
    executor.select_contract = Mock(side_effect=EntryBlocked('Insufficient cash'))
    executor.broker = Mock()
    now = datetime.now(timezone.utc)
    executor.execute_pending(now)
    store.cancel_reserved.assert_called_once_with(pending, now, 'Insufficient cash')
    store.event.assert_called_once()
    store.journal.reserve.assert_not_called()
    executor.broker.submit.assert_not_called()


def test_cash_refusal_sends_contract_and_amounts_to_alert():
    from decimal import Decimal
    from unittest.mock import patch
    from trading.ema_broker import KiteBroker
    from trading.ema_execution import EntryBlocked
    kite = Mock()
    kite.positions.return_value = {'net': []}
    kite.orders.return_value = []
    kite.margins.return_value = {'net': 1000, 'available': {'cash': 1000, 'live_balance': 1000}}
    store = SimpleNamespace(user_id=123, key='bullish_calls')
    broker = KiteBroker(kite, store)
    broker.premium = Mock(return_value=Decimal('100'))
    contract = {'tradingsymbol': 'NIFTYTESTCE', 'side': 'CE'}
    now = datetime.now(timezone.utc)
    with patch('trading.order_failure_alerts.notify_insufficient_cash') as alert:
        with pytest.raises(EntryBlocked):
            broker.check_entry(contract, 65, now, None)
        alert.assert_called_once_with(123, 'bullish_calls', contract, 65, Decimal('100'),
                                      Decimal('1000'), Decimal('6700'), now)
    kite.place_order.assert_not_called()


def test_cash_alert_message_and_separate_cooldown():
    from decimal import Decimal
    from unittest.mock import patch
    from trading.order_failure_alerts import notify_insufficient_cash
    with patch('trading.order_failure_alerts.notify_order_failure') as send:
        notify_insufficient_cash(123, 'bullish_calls', {'tradingsymbol':'NIFTYTESTCE', 'side':'CE'},
            65, Decimal('100'), Decimal('1000'), Decimal('6700'), datetime.now(timezone.utc))
        message = send.call_args.kwargs['message']
        assert '₹5,700.00' in message
        assert '₹6,700.00' in message
        assert '₹1,000.00' in message
        assert 'NIFTYTESTCE' in message
        assert 'No order submitted' in message
        assert send.call_args.kwargs['cooldown_key'] == 'cash:Kite:bullish_calls'
