from datetime import datetime, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

spec = spec_from_file_location("stock_notification_persistence", Path(__file__).parents[1] / "trading" / "notifications.py")
module = module_from_spec(spec)
spec.loader.exec_module(module)
notification_rows = module.notification_rows


def test_stock_alert_has_rails_required_session_date_in_exchange_timezone():
    rows = notification_rows(7, "proximity", [{"symbol": "infy", "ltp": 1400}],
                             datetime(2026, 11, 20, 19, 0, tzinfo=timezone.utc))
    assert len(rows) == 1
    assert rows[0]["session_date"].isoformat() == "2026-11-21"
    assert rows[0]["symbol"] == "INFY"
    assert len(rows[0]["event_key"]) == 64
