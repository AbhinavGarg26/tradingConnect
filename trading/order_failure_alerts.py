"""Account/broker scoped market-order failure alerts, shared across processes."""
import html
import logging
from functools import wraps

log = logging.getLogger(__name__)
COOLDOWN_SECONDS = 300


def notify_order_failure(user_id, broker, params, error):
    # Monitoring must never replace the original order exception or retry an order.
    try:
        from sqlalchemy import text
        from trading.database import get_db
        from trading.alerts import Alerter
        from trading.service_runtime import ServiceRuntimeMonitor
        with get_db() as db:
            claimed = db.execute(text("""
                INSERT INTO market_live_state
                    (entity_type, entity_key, metric_type, metric_key, payload,
                     event_time, is_complete, created_at, updated_at)
                VALUES ('USER', :user, 'ORDER_FAILURE_ALERT', :broker, '{}'::jsonb,
                        NOW(), TRUE, NOW(), NOW())
                ON CONFLICT (entity_type, entity_key, metric_type, metric_key)
                DO UPDATE SET event_time=NOW(), updated_at=NOW()
                WHERE market_live_state.event_time <= NOW() - INTERVAL '5 minutes'
                RETURNING entity_key
            """), {'user': str(user_id), 'broker': broker}).first()
            if not claimed:
                return
        # Commit the atomic cooldown claim before contacting Telegram.
        escape = lambda value: html.escape(str(value))
        message = (
            f"🚨 <b>{escape(broker)} market order failed</b>\n"
            f"Symbol: {escape(params.get('tradingsymbol') or params.get('trading_symbol') or 'unknown')}\n"
            f"Side: {escape(params.get('transaction_type', 'unknown'))} | "
            f"Quantity: {escape(params.get('quantity', 'unknown'))}\n"
            f"Error: {escape(ServiceRuntimeMonitor._redact(str(error))[:1000])}\n"
            "Verify broker orders and positions before retrying; submission outcome may be uncertain.\n"
            "Further failure alerts for this broker are muted for 5 minutes."
        )
        with get_db() as db:
            if not Alerter.from_db(db, user_id).send(message):
                log.warning('Market-order failure Telegram delivery failed for %s', broker)
    except Exception:
        log.exception('Could not send market-order failure alert for %s', broker)


def instrument_kite(kite, user_id):
    """Cover SDK and legacy protected-market submissions at the common endpoint."""
    if getattr(kite, '_order_failure_alerts_installed', False):
        return kite
    original = kite._post

    @wraps(original)
    def post(route, *args, **kwargs):
        try:
            return original(route, *args, **kwargs)
        except Exception as exc:
            params = kwargs.get('params') or {}
            if route == 'order.place' and params.get('order_type') == 'MARKET':
                notify_order_failure(user_id, 'Kite', params, exc)
            raise

    kite._post = post
    kite._order_failure_alerts_installed = True
    return kite
