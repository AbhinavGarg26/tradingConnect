"""Read-only combined broker reports with durable, at-most-once delivery."""
import logging
import math
import threading
from html import escape

import httpx
from sqlalchemy import text

from market.groww_entry_price import execution_entry_price
from market.kite_ltp import live_prices

log = logging.getLogger(__name__)


def groww_totals(rows, prices, orders):
    """Never invent a zero when a P&L component or quote is unavailable."""
    realised = unrealised = 0.0
    for row in rows:
        reported = next((row[k] for k in ('realised_pnl', 'realized_pnl', 'realized')
                         if row.get(k) is not None), None)
        if reported is None:
            return None
        realised += float(reported)
        quantity = int(row.get('quantity') or row.get('net_quantity') or 0)
        if quantity:
            key = (str(row.get('exchange') or 'NSE').upper(), row['segment'],
                   str(row.get('trading_symbol') or row.get('symbol') or ''))
            price = prices.get(key)
            # Groww net_price may include booked P&L. Reconcile actual fills
            # instead; an overnight/unreconciled position remains unavailable.
            entry = execution_entry_price(orders.get(row['segment'], []), row)
            if price is None or entry is None:
                return None
            unrealised += (price - entry) * float(row.get('quantity') or row.get('net_quantity') or 0)
    if not all(math.isfinite(value) for value in (realised, unrealised)):
        return None
    return dict(realised_pnl=realised, unrealised_pnl=unrealised, current_pnl=realised + unrealised)


def groww_snapshot(db, user_id, kite):
    from trading.exchange_link import ExchangeLinkRepo
    link = ExchangeLinkRepo.get_for_user(db, user_id, provider='groww')
    if not link or not link.is_session_valid:
        return None
    token = link.decrypt_session_token(db)
    if not token:
        return None
    with httpx.Client(base_url='https://api.groww.in/v1',
                      headers={'Authorization': f'Bearer {token}', 'X-API-VERSION': '1.0'}, timeout=5) as client:
        def fetch(path, **params):
            response = client.get(path, params=params)
            response.raise_for_status()
            body = response.json()
            if body.get('status') != 'SUCCESS' or not isinstance(body.get('payload'), dict):
                raise ValueError('Incomplete Groww response')
            return body['payload']
        rows, orders = [], {}
        for segment in ('CASH', 'FNO'):
            positions = fetch('/positions/user', segment=segment).get('positions')
            if not isinstance(positions, list):
                return None
            rows.extend(dict(row, segment=segment) for row in positions)
            if any(float(row.get('quantity') or row.get('net_quantity') or 0) for row in positions):
                segment_orders = []
                for page in range(20):
                    batch = fetch('/order/list', segment=segment, page=page, page_size=100).get('order_list')
                    if not isinstance(batch, list):
                        return None
                    segment_orders.extend(batch)
                    if len(batch) < 100:
                        break
                else:
                    return None  # Never calculate entry from a truncated book.
                orders[segment] = segment_orders
    return groww_totals(rows, live_prices(kite, rows), orders)


def report_message(kite_funds, groww, checkpoint, now):
    lines = [f'📊 <b>Portfolio summary — {escape(checkpoint)} IST</b>',
             now.strftime('%d %b %Y, %H:%M IST'), '']
    for label, data in (('Kite', kite_funds), ('Groww', groww)):
        if data is None:
            lines.append(f'<b>{label}</b>: P&amp;L unavailable/incomplete')
        else:
            lines += [f"<b>{label}</b> · Session P&amp;L ₹{data['current_pnl']:,.2f}",
                      f"Realised ₹{data['realised_pnl']:,.2f} | Open ₹{data['unrealised_pnl']:,.2f}"]
    if groww is not None:
        lines += ['', f"<b>Combined P&amp;L: ₹{kite_funds['current_pnl'] + groww['current_pnl']:,.2f}</b>",
                  f"Realised ₹{kite_funds['realised_pnl'] + groww['realised_pnl']:,.2f} | "
                  f"Open ₹{kite_funds['unrealised_pnl'] + groww['unrealised_pnl']:,.2f}"]
    else:
        lines += ['', '<b>Combined P&amp;L</b>: unavailable (Groww incomplete)']
    lines += [f"Kite funds ₹{kite_funds['total_funds']:,.2f} | Available ₹{kite_funds['available_balance']:,.2f}",
              'Broker session figures; fees may be excluded. Quotes captured during report generation.']
    return '\n'.join(lines)


def dispatch_summary(factory, user_id, checkpoint, now, kite_funds, alerter, snapshot_loader=None):
    """Claim before the external send. Timeout/crash never causes automatic replay."""
    if not alerter._enabled:
        return False
    # get_db commits before returning from this block, independently of the
    # caller's market-monitor transaction and independently of process restarts.
    with factory() as db:
        delivery_id = db.execute(text("""
            INSERT INTO portfolio_summary_deliveries
                (user_id, session_date, checkpoint, status, created_at, updated_at)
            VALUES (:user_id, :session_date, :checkpoint, 'sending', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT (user_id, session_date, checkpoint) DO NOTHING RETURNING id
        """), {'user_id': user_id, 'session_date': now.date(), 'checkpoint': checkpoint}).scalar()
    if delivery_id is None:
        return False
    groww = None
    try:
        with factory() as db:
            if snapshot_loader:
                groww = snapshot_loader(db, user_id)
            else:
                from trading.exchange_link import ExchangeLinkRepo
                # A separate broker client avoids sharing the live-monitor
                # client's mutable HTTP session with the report worker.
                kite = ExchangeLinkRepo.get_kite_client(db, user_id)
                groww = groww_snapshot(db, user_id, kite)
    except Exception as exc:
        log.warning('Groww summary incomplete (%s)', type(exc).__name__)
    status = 'uncertain'
    try:
        response = httpx.post(alerter.TELEGRAM_URL.format(token=alerter.bot_token),
            json={'chat_id': alerter.chat_id, 'text': report_message(kite_funds, groww, checkpoint, now),
                  'parse_mode': 'HTML'}, timeout=5)
        if response.status_code == 200 and response.json().get('ok') is True:
            status = 'sent'
        elif response.status_code < 500:
            status = 'failed'
    except Exception as exc:
        log.warning('Portfolio summary outcome uncertain (%s)', type(exc).__name__)
    with factory() as db:
        db.execute(text('UPDATE portfolio_summary_deliveries SET status=:status, updated_at=CURRENT_TIMESTAMP WHERE id=:id'),
                   {'status': status, 'id': delivery_id})
    return status == 'sent'


def queue_summary(user_id, checkpoint, now, funds, alerter):
    def work():
        try:
            from trading.database import get_db
            dispatch_summary(get_db, user_id, checkpoint, now, funds, alerter)
        except Exception as exc:
            log.warning('Portfolio summary worker failed (%s)', type(exc).__name__)
    threading.Thread(target=work, name='portfolio-summary', daemon=True).start()
