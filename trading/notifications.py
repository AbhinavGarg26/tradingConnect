"""Persistent account-scoped stock inbox. Rails owns the table migration."""
import hashlib
import json
from datetime import timezone
from sqlalchemy import text


def notification_rows(user_id, kind, items, captured_at):
    if captured_at.tzinfo is None:
        raise ValueError("Notification time must include a timezone")
    captured_at = captured_at.astimezone(timezone.utc)
    rows = []
    for item in items:
        payload = json.dumps(item, sort_keys=True, default=str, allow_nan=False)
        symbol = str(item["symbol"]).upper()
        identity = json.dumps([str(user_id), symbol, kind, captured_at.isoformat(), payload])
        rows.append(dict(user_id=user_id, symbol=symbol, kind=kind,
                         event_key=hashlib.sha256(identity.encode()).hexdigest(),
                         payload=payload, captured_at=captured_at))
    return rows


def persist_stock_notifications(user_id, kind, items, captured_at):
    from trading.database import get_db
    rows = notification_rows(user_id, kind, items, captured_at)
    if not rows:
        return
    # Commit the whole batch before the proximity sender advances its cooldown.
    with get_db() as db:
        db.execute(text("""
            INSERT INTO stock_notifications
                (user_id, symbol, kind, event_key, payload, captured_at, created_at, updated_at)
            VALUES (:user_id, :symbol, :kind, :event_key, CAST(:payload AS jsonb),
                    :captured_at, NOW(), NOW())
            ON CONFLICT (user_id, event_key) DO NOTHING
        """), rows)
