"""Account-scoped stock inbox. Rails owns DDL; callers own transactions."""
import hashlib
import json
from datetime import timezone
from zoneinfo import ZoneInfo

from sqlalchemy import text

KINDS = {"volume_surge", "daily_volume", "price_movement", "proximity", "support_rejection"}


def notification_rows(user_id, kind, items, captured_at):
    if user_id is None or kind not in KINDS:
        raise ValueError("Account and supported notification kind are required")
    if captured_at.tzinfo is None:
        raise ValueError("Notification time must include a timezone")
    captured_at = captured_at.astimezone(timezone.utc)
    rows = []
    for item in items:
        symbol = str(item["symbol"]).strip().upper()
        if not symbol:
            raise ValueError("Stock symbol is required")
        # The proximity identity survives retries across scan times, including
        # a restart after insertion but before the scanner commits its cooldown.
        if kind == "proximity":
            identity = [item["metric_key"], str(item["expires_at"]), item["alert_count"]]
        else:
            identity = [captured_at.replace(second=0, microsecond=0).isoformat(), item]
        key = json.dumps([str(user_id), symbol, kind, identity], sort_keys=True, default=str)
        rows.append({"user_id": user_id, "symbol": symbol, "kind": kind,
                     "event_key": hashlib.sha256(key.encode()).hexdigest(),
                     "payload": json.dumps(item, sort_keys=True, default=str, allow_nan=False),
                     "captured_at": captured_at,
                     "session_date": captured_at.astimezone(ZoneInfo("Asia/Kolkata")).date()})
    return rows


def persist_stock_notifications(db, user_id, kind, items, captured_at):
    rows = notification_rows(user_id, kind, items, captured_at)
    if not rows:
        return
    # A failed insert must not poison the market-scanner transaction. On success,
    # inbox records and the scanner's proximity cooldown commit atomically.
    with db.begin_nested():
        db.execute(text("""
            INSERT INTO stock_notifications
              (user_id, symbol, kind, event_key, payload, captured_at, session_date, created_at, updated_at)
            VALUES (:user_id, :symbol, :kind, :event_key, CAST(:payload AS jsonb),
                    :captured_at, :session_date, NOW(), NOW())
            ON CONFLICT (user_id, event_key) DO NOTHING
        """), rows)
