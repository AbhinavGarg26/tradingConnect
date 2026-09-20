from datetime import datetime, timedelta, timezone
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("sqlalchemy", minversion="2.0")

from trading.service_runtime import ServiceRuntimeMonitor


def test_token_state_reports_missing_link():
    db = Mock()
    db.execute.return_value.mappings.return_value.first.return_value = None

    assert ServiceRuntimeMonitor("test", Mock())._token_state(db, 1) == ("missing", None)


def test_token_state_reports_expired_token():
    expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db = Mock()
    db.execute.return_value.mappings.return_value.first.return_value = {
        "session_token_encrypted": b"encrypted",
        "session_expires_at": expires_at,
    }

    status, returned_expiry = ServiceRuntimeMonitor("test", Mock())._token_state(db, 1)

    assert status == "expired"
    assert returned_expiry == expires_at


def test_market_session_token_alert_window_uses_india_time():
    ist = ZoneInfo("Asia/Kolkata")

    assert ServiceRuntimeMonitor._nse_market_session_active(
        datetime(2026, 9, 21, 9, 15, tzinfo=ist)
    )
    assert not ServiceRuntimeMonitor._nse_market_session_active(
        datetime(2026, 9, 20, 10, 0, tzinfo=ist)
    )


def test_next_token_alert_moves_weekend_reminder_to_monday_open():
    class Result:
        def scalar(self):
            return None

    class Db:
        def execute(self, *_args, **_kwargs):
            return Result()

    ist = ZoneInfo("Asia/Kolkata")
    now = datetime(2026, 9, 20, 18, 0, tzinfo=ist)  # Sunday
    next_at = ServiceRuntimeMonitor._next_token_alert_at(Db(), now, now, 6)

    assert next_at.astimezone(ist) == datetime(2026, 9, 21, 9, 15, tzinfo=ist)


def test_next_token_alert_skips_configured_holidays():
    class Result:
        def __init__(self, holiday):
            self.holiday = holiday

        def scalar(self):
            return 1 if self.holiday else None

    class Db:
        def execute(self, _query, params):
            return Result(params["holiday_date"].isoformat() == "2026-09-21")

    ist = ZoneInfo("Asia/Kolkata")
    now = datetime(2026, 9, 20, 18, 0, tzinfo=ist)  # Sunday
    next_at = ServiceRuntimeMonitor._next_token_alert_at(Db(), now, now, 6)

    assert next_at.astimezone(ist) == datetime(2026, 9, 22, 9, 15, tzinfo=ist)
