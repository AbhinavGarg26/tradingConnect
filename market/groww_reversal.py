"""Groww execution adapter for the shared reversal state machine.

Market data stays on Kite; all positions, funds and orders stay on Groww.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo
from market.entry_price_tracker import CurrentEntryPriceTracker
from market.groww_entry_price import execution_entry_price
from market.groww_position_risk import GrowwMarketExitExecutor


STATUS = {'COMPLETED': 'COMPLETE', 'EXECUTED': 'COMPLETE', 'FAILED': 'REJECTED',
          'EXPIRED': 'CANCELLED'}
TERMINAL = {'COMPLETE', 'CANCELLED', 'REJECTED'}


class GrowwReversalAdapter:
    def __init__(self, kite, token, logger, user_id):
        self.market_data = kite
        self.client = GrowwMarketExitExecutor(token, logger, user_id)
        self._instruments = None
        self._day = None
        self.raw_positions = {}
        self.raw_orders = []

    def instruments(self):
        day = datetime.now(ZoneInfo('Asia/Kolkata')).date()
        if self._day != day:
            self._instruments = self.market_data.instruments()
            self._day = day
        return self._instruments

    def quote(self, *args, **kwargs):
        return self.market_data.quote(*args, **kwargs)

    def ltp(self, *args, **kwargs):
        return self.market_data.ltp(*args, **kwargs)

    def historical_data(self, *args, **kwargs):
        return self.market_data.historical_data(*args, **kwargs)

    def orders(self):
        raw = []
        for page in range(501):
            rows = self.client._get('/order/list', {'segment': 'FNO', 'page': page, 'page_size': 100})['order_list']
            raw.extend(rows)
            if len(rows) < 100:
                break
        else:
            raise RuntimeError('Groww order history pagination incomplete')
        self.raw_orders = raw
        normalized = []
        for row in raw:
            status = STATUS.get(row['order_status'].upper(), row['order_status'].upper())
            # Groww documents DAY validity. Cancel any remaining reversal BUY on
            # reconciliation, including partial fills; never resubmit its remainder.
            ref = str(row.get('order_reference_id') or '')
            if ref.startswith('RV') and row.get('transaction_type') == 'BUY' and status not in TERMINAL:
                if status not in ('CANCELLATION_REQUESTED', 'CANCEL_REQUESTED'):
                    self.client._request('POST', '/order/cancel', {'segment': 'FNO', 'groww_order_id': row['groww_order_id']})
            normalized.append({**row, 'order_id': str(row['groww_order_id']), 'status': status,
                'tag': ref, 'tradingsymbol': row['trading_symbol'], 'exchange': 'NFO'})
        return normalized

    def positions(self):
        instruments = {(i['exchange'], i['tradingsymbol']): i for i in self.instruments()}
        self.orders()  # complete execution ledger, also cancels remaining reversal BUYs
        rows = self.client._get('/positions/user', {'segment': 'FNO'})['positions']
        self.raw_positions = {}
        normalized = []
        prices = self.market_data.ltp([f"NFO:{r['trading_symbol']}" for r in rows if int(r.get('quantity') or 0)]) if any(int(r.get('quantity') or 0) for r in rows) else {}
        for row in rows:
            symbol = row['trading_symbol']
            quantity = int(row.get('quantity') or 0)
            product = row['product']
            if row.get('exchange') != 'NSE':
                if quantity:
                    raise RuntimeError('Unsupported Groww exchange; reversal risk snapshot incomplete')
                continue
            instrument = instruments.get(('NFO', symbol))
            if quantity and not instrument:
                raise RuntimeError(f'Groww contract has no exact Kite instrument match: {symbol}')
            orders = [o for o in self.raw_orders if o.get('trading_symbol') == symbol
                      and o.get('exchange') == 'NSE' and o.get('product') == product]
            buys = [o for o in orders if o.get('transaction_type') == 'BUY']
            pnl = float(row['realised_pnl'])  # Missing risk fields must fail closed.
            if quantity:
                price = float(prices.get(f'NFO:{symbol}', {}).get('last_price') or 0)
                if price <= 0:
                    raise RuntimeError(f'No fresh Groww-position market price: {symbol}')
                entry = execution_entry_price(self.raw_orders, row)
                if entry is None:
                    raise RuntimeError(f'Groww risk snapshot cannot reconcile fills: {symbol}')
                pnl += (price - entry) * quantity
            p = dict(exchange='NFO', tradingsymbol=symbol, product=product, quantity=quantity,
                     instrument_token=int(instrument['instrument_token']) if instrument else 0,
                     buy_quantity=sum(int(o.get('filled_quantity') or 0) for o in buys),
                     buy_value=sum(int(o.get('filled_quantity') or 0)*float(o.get('average_fill_price') or 0) for o in buys), pnl=pnl)
            self.raw_positions[CurrentEntryPriceTracker.position_key(p)] = {**row, 'segment': 'FNO'}
            normalized.append(p)
        return {'net': normalized}

    def margins(self, _segment):
        payload = self.client._get('/margins/detail/user', {})
        funds = float(payload['fno_margin_details']['option_buy_balance_available'])
        return {'net': funds, 'available': {'live_balance': funds}}

    def order_margins(self, orders):
        body = [self._order(o) for o in orders]
        result = self.client._request('POST', '/margins/detail/orders?segment=FNO', body)
        return [{'total': float(result['total_requirement'])}]

    @staticmethod
    def _order(params):
        if params['exchange'] != 'NFO':
            raise ValueError('Groww reversals support NSE FNO only')
        return dict(trading_symbol=params['tradingsymbol'], exchange='NSE',
                    quantity=params['quantity'], product=params['product'],
                    order_type=params['order_type'], transaction_type=params['transaction_type'],
                    price=params['price'])

    def place_order(self, **params):
        body = self._order(params)
        body.update(segment='FNO', validity='DAY', order_reference_id=params['tag'])
        return str(self.client._request('POST', '/order/create', body)['groww_order_id'])


class GrowwReversalEntries:
    position_key = staticmethod(CurrentEntryPriceTracker.position_key)

    def __init__(self, adapter):
        self.adapter = adapter

    def resolve(self, _broker, position):
        row = self.adapter.raw_positions[self.position_key(position)]
        entry = execution_entry_price(self.adapter.raw_orders, row)
        if entry is None or entry <= 0:
            raise RuntimeError('Groww fills cannot reconcile to open position')
        return entry, False


class GrowwReversalExits:
    def __init__(self, adapter):
        self.adapter = adapter

    def remove_missing(self, active_keys):
        # The underlying executor uses Groww-native exchange and segment keys.
        self.adapter.client.remove_missing({self.adapter.client._position_key(self._native(p))
            for k, p in self.adapter.raw_positions.items() if k in active_keys and p.get('quantity')})

    @staticmethod
    def _native(p):
        return dict(exchange='NSE', segment='FNO', tradingsymbol=p.get('tradingsymbol') or p['trading_symbol'],
                    product=p['product'], quantity=p['quantity'])

    def remove_legacy_gtts(self, position):
        # Listing can return IDs only. Resolve full smart-order details before
        # deciding whether a GTT/OCO belongs to this exact position.
        for kind in ('GTT', 'OCO'):
            for page in range(501):
                rows = self.adapter.client._get('/order-advance/list', dict(segment='FNO',
                    smart_order_type=kind, status='ACTIVE', page=page, page_size=50))['orders']
                for row in rows:
                    order_id = row['smart_order_id']
                    detail = self.adapter.client._get(f'/order-advance/status/FNO/{kind}/internal/{order_id}', {})
                    if detail['trading_symbol'] != position['tradingsymbol'] or detail['exchange'] != 'NSE':
                        continue
                    product = detail['product_type'] if 'product_type' in detail else detail['product']
                    if product != position['product']:
                        continue
                    self.adapter.client._request('POST', f'/order-advance/cancel/FNO/{kind}/{order_id}', {})
                    return False  # re-read until cancellation is confirmed by list
                if len(rows) < 50:
                    break
            else:
                raise RuntimeError('Groww smart-order pagination incomplete')
        return True

    def exit_position(self, position, reason, reference_price=None):
        return self.adapter.client.exit_position(self._native(position), reason)
