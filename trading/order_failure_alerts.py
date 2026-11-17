"""Account/broker scoped market-order failure alerts, shared across processes."""
import html
import logging
from functools import wraps

log = logging.getLogger(__name__)
COOLDOWN_SECONDS = 300


def notify_order_failure(user_id, broker, params, error, *, message=None, cooldown_key=None):
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
            """), {'user': str(user_id), 'broker': cooldown_key or broker}).first()
            if not claimed:
                return
        # Commit the atomic cooldown claim before contacting Telegram.
        escape = lambda value: html.escape(str(value))
        message = message or (
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


def notify_insufficient_cash(user_id, strategy_key, contract, quantity, premium,
                             available, required, observed_at):
    """Alert on a refused live entry, independently of submission-error alerts."""
    try:
        from decimal import Decimal
        escape = lambda value: html.escape(str(value))
        label = {
            'nifty_bullish_15m_9_21': 'NIFTY 15m EMA 9/21 Calls',
            'bullish_calls': 'NIFTY 15m EMA 9/21 Calls',
        }.get(strategy_key, strategy_key)
        shortfall = max(Decimal(0), required - available)
        message = (
            '⚠️ <b>Insufficient cash — live entry skipped</b>\n'
            f'Strategy: {escape(label)}\nBroker: Kite | Mode: LIVE\n'
            f'Contract: {escape(contract["tradingsymbol"])} | BUY {escape(contract.get("side", ""))}\n'
            f'Quantity: {quantity} units | Premium estimate: ₹{premium:,.2f}\n'
            f'Premium cost: ₹{premium * quantity:,.2f} | Charge buffer: ₹200.00\n'
            f'Available cash used for check: ₹{available:,.2f}\n'
            f'Required cash: ₹{required:,.2f}\nShortfall: ₹{shortfall:,.2f}\n'
            f'Time: {escape(observed_at.isoformat())}\n'
            'No order submitted. This signal is skipped; service continues.\n'
            'Insufficient-cash alerts for this strategy are muted for 5 minutes.'
        )
        notify_order_failure(user_id, 'Kite', {}, '', message=message,
                             cooldown_key=f'cash:Kite:{strategy_key}')
    except Exception:
        log.exception('Could not prepare insufficient-cash alert')
