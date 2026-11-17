from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, MagicMock, patch

import pytest
from trading.alerts import Alerter
from trading.notifications import notification_rows, persist_stock_notifications

NOW = datetime(2026, 11, 17, 5, 0, tzinfo=timezone.utc)


def test_proximity_identity_survives_scan_retry_but_not_second_alert():
    item = dict(symbol='RELIANCE', metric_key='7:ema:65', expires_at=NOW + timedelta(hours=1), alert_count=0, ltp=100)
    first = notification_rows(7, 'proximity', [item], NOW)[0]
    retry = notification_rows(7, 'proximity', [dict(item, ltp=101)], NOW + timedelta(minutes=1))[0]
    second = notification_rows(7, 'proximity', [dict(item, alert_count=1)], NOW)[0]
    assert first['event_key'] == retry['event_key']
    assert first['event_key'] != second['event_key']
    assert first['event_key'] != notification_rows(8, 'proximity', [item], NOW)[0]['event_key']


def test_session_date_uses_ist_and_invalid_timestamp_rejected():
    when = datetime(2026, 11, 16, 20, 0, tzinfo=timezone.utc)
    row = notification_rows(7, 'price_movement', [dict(symbol=' abc ', ltp=100)], when)[0]
    assert str(row['session_date']) == '2026-11-17'
    assert row['symbol'] == 'ABC'
    with pytest.raises(ValueError):
        notification_rows(7, 'price_movement', [dict(symbol='ABC')], when.replace(tzinfo=None))
    with pytest.raises(ValueError):
        notification_rows(7, 'price_movement', [dict(symbol='ABC', ltp=float('nan'))], when)


def test_inbox_and_cooldown_share_outer_transaction():
    db = MagicMock()
    persist_stock_notifications(db, 7, 'price_movement', [dict(symbol='ABC', ltp=100)], NOW)
    db.begin_nested.assert_called_once()
    assert 'ON CONFLICT' in str(db.execute.call_args.args[0])
    db.commit.assert_not_called()


@pytest.mark.parametrize('method', ['volume_surges', 'daily_volume_rvols', 'price_movements', 'proximity_alerts'])
def test_stock_routes_never_call_telegram(method):
    alerter = Alerter(bot_token='test', chat_id='test')
    alerter.user_id, alerter._notification_db = 7, Mock()
    with patch('trading.notifications.persist_stock_notifications') as save, patch.object(alerter, 'send') as telegram:
        assert getattr(alerter, method)([dict(symbol='ABC')], NOW)
        save.assert_called_once()
        telegram.assert_not_called()


def test_failed_insert_returns_false_without_telegram_fallback():
    alerter = Alerter(bot_token='test', chat_id='test')
    alerter.user_id, alerter._notification_db = 7, Mock()
    with patch('trading.notifications.persist_stock_notifications', side_effect=RuntimeError('db')), patch.object(alerter, 'send') as telegram:
        assert not alerter.price_movements([dict(symbol='ABC')], NOW)
        telegram.assert_not_called()


def test_from_db_preserves_notification_account_and_session():
    db = Mock()
    with patch('trading.repositories.MarketConfigRepo.get', return_value=''):
        alerter = Alerter.from_db(db, 99)
    assert alerter.user_id == 99
    assert alerter._notification_db is db
