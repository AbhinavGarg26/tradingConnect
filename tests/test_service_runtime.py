from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

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
