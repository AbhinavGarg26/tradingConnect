"""Execution adapter for frozen 30m V3. No experimental signal rules."""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import time

from market.kite_orders import place_protected_market_order

TAG = 'NIFTY30REFV3'
FINAL = {'COMPLETE', 'REJECTED', 'CANCELLED'}


class ConfirmedInsufficientFunds(RuntimeError):
    """Only raised for a broker-confirmed rejection with zero filled quantity."""


class UncertainOrder(RuntimeError):
    pass


def funds_message(message):
    value = str(message or '').lower()
    return 'insufficient funds' in value or 'margin exceeds' in value


def select_offset_contract(instruments, today, side, spot, positions, orders):
    """ATM +/- two listed strikes, then lower unoccupied strikes for either side."""
    catalog = [i for i in instruments if i.get('name') == 'NIFTY'
               and i.get('instrument_type') in {'CE', 'PE'} and i.get('expiry')
               and i['expiry'] >= today]
    if not catalog or side not in {'CE', 'PE'}:
        raise ValueError('No current NIFTY option expiry/side available')
    expiry = min(i['expiry'] for i in catalog)
    chain = sorted([i for i in catalog if i['expiry'] == expiry and i['instrument_type'] == side],
                   key=lambda i: Decimal(str(i['strike'])))
    atm = min(range(len(chain)), key=lambda n: (abs(Decimal(str(chain[n]['strike']))-spot),
                                               Decimal(str(chain[n]['strike']))))
    selected = atm + (2 if side == 'CE' else -2)
    if selected < 0 or selected >= len(chain):
        raise ValueError('Two-strike offset unavailable in current instrument list')
    by_symbol = {i['tradingsymbol']: i for i in catalog}
    occupied = set()
    for item in positions:
        if item.get('exchange') == 'NFO' and int(item.get('quantity') or 0) != 0:
            contract = by_symbol.get(item.get('tradingsymbol'))
            if contract:
                occupied.add(Decimal(str(contract['strike'])))
    for item in orders:
        if item.get('exchange') == 'NFO' and item.get('status') not in FINAL:
            contract = by_symbol.get(item.get('tradingsymbol'))
            if contract:
                occupied.add(Decimal(str(contract['strike'])))
    # Block the strike across CE/PE and all listed unexpired NIFTY expiries.
    for contract in reversed(chain[:selected+1]):
        if Decimal(str(contract['strike'])) not in occupied:
            if int(contract['lot_size']) < 1:
                raise ValueError('Invalid contract lot size')
            return contract
    raise RuntimeError('No unoccupied lower strike available')


def confirm_market_order(kite, contract, transaction, quantity, journal, *, exit_order=False,
                         sleep_fn=time.sleep, attempts=20):
    """Submit once. Never retry an ambiguous submission or partially filled order."""
    try:
        order_id = place_protected_market_order(
            kite, variety=kite.VARIETY_REGULAR, exchange='NFO',
            tradingsymbol=contract['tradingsymbol'], transaction_type=transaction,
            quantity=quantity, product=kite.PRODUCT_MIS, order_type=kite.ORDER_TYPE_MARKET,
            validity=kite.VALIDITY_DAY, tag=TAG,
        )
    except Exception as exc:
        # Kite documents MarginException as a definitive order-placement rejection.
        if not exit_order and type(exc).__name__ == 'MarginException':
            raise ConfirmedInsufficientFunds(str(exc)) from exc
        raise UncertainOrder(f'Order submission failed ({type(exc).__name__}); reconcile before retry') from exc
    try:
        journal.order_submitted(str(order_id), exit_order=exit_order)
    except Exception as exc:
        raise UncertainOrder(f'Order {order_id} sent but journal update failed') from exc
    for _ in range(attempts):
        history = kite.order_history(order_id)
        if not history:
            raise UncertainOrder(f'No history for submitted order {order_id}')
        state = history[-1]
        filled = int(state.get('filled_quantity') or 0)
        if state['status'] == 'COMPLETE':
            premium = Decimal(str(state.get('average_price') or 0))
            if filled != quantity or not premium.is_finite() or premium <= 0:
                raise UncertainOrder(f'Order {order_id} completed with unexpected fill details')
            return str(order_id), premium
        if state['status'] in {'REJECTED', 'CANCELLED'}:
            message = str(state.get('status_message') or state.get('status_message_raw') or '')
            if (not exit_order and state['status'] == 'REJECTED' and filled == 0
                    and int(state.get('pending_quantity') or 0) == 0 and funds_message(message)):
                raise ConfirmedInsufficientFunds(message)
            raise UncertainOrder(f'Order {order_id} {state["status"]}, filled={filled}: {message}')
        sleep_fn(1)
    raise UncertainOrder(f'Order {order_id} not confirmed; no retry permitted')


def fresh_price(kite, symbol, now, *, max_age_seconds=25):
    quote = kite.quote([symbol]).get(symbol)
    if not quote:
        raise RuntimeError(f'Quote missing for {symbol}')
    timestamp = quote.get('timestamp')
    if not isinstance(timestamp, datetime):
        raise RuntimeError(f'Timestamp missing for {symbol} quote')
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=now.tzinfo)
    age = (now-timestamp).total_seconds()
    price = Decimal(str(quote.get('last_price') or 0))
    if age < -5 or age > max_age_seconds or not price.is_finite() or price <= 0:
        raise RuntimeError(f'Stale or invalid quote for {symbol}')
    return price


def contract_quantity(kite, contract):
    return sum(int(p.get('quantity') or 0) for p in kite.positions().get('net', [])
               if p.get('exchange') == 'NFO' and p.get('tradingsymbol') == contract['tradingsymbol'])


def assert_no_contract_orders(kite, contract):
    if any(o.get('exchange') == 'NFO' and o.get('tradingsymbol') == contract['tradingsymbol']
           and o.get('status') not in FINAL for o in kite.orders()):
        raise UncertainOrder('Pending broker order on selected contract; reconcile')


@dataclass
class OwnedPosition:
    contract: dict
    quantity: int
    paper: bool


class ReferenceExecutor:
    def __init__(self, kite, instruments, journal, *, live, now_fn):
        self.kite, self.instruments, self.journal = kite, instruments, journal
        self.live, self.now_fn = live, now_fn
        self.position = None

    def handle(self, signal, engine):
        if signal.action.startswith('BUY_'):
            return self._enter(signal, engine)
        return self._exit(signal, engine)

    def _enter(self, signal, engine):
        if self.position is not None:
            raise RuntimeError('Entry while position already owned')
        now = self.now_fn()
        spot = fresh_price(self.kite, 'NSE:NIFTY 50', now)
        contract = select_offset_contract(self.instruments, now.date(), signal.action[-2:], spot,
                                         self.kite.positions().get('net', []), self.kite.orders())
        quantity = int(contract['lot_size'])  # One exchange lot.
        symbol = 'NFO:' + contract['tradingsymbol']
        premium = fresh_price(self.kite, symbol, self.now_fn())
        self.journal.begin_entry(signal, contract, quantity, engine.entry_count+1, spot,
                                 paper=not self.live)
        paper, order_id, reason = not self.live, None, None
        if self.live:
            assert_no_contract_orders(self.kite, contract)
            if contract_quantity(self.kite, contract) != 0:
                raise UncertainOrder('Selected contract became occupied before submission')
            try:
                order_id, premium = confirm_market_order(
                    self.kite, contract, self.kite.TRANSACTION_TYPE_BUY, quantity, self.journal)
            except ConfirmedInsufficientFunds as exc:
                # Confirm no live position/order remains before entering a simulated trade.
                assert_no_contract_orders(self.kite, contract)
                if contract_quantity(self.kite, contract) != 0:
                    raise UncertainOrder('Position exists after funds rejection; paper fallback forbidden')
                paper, reason = True, str(exc)
                premium = fresh_price(self.kite, symbol, self.now_fn())
        # Record a confirmed broker fill before any later market-data request can fail.
        self.journal.entry_filled(order_id, premium, spot, paper=paper, fallback_reason=reason)
        self.position = OwnedPosition(contract, quantity, paper)
        index_at_fill = fresh_price(self.kite, 'NSE:NIFTY 50', self.now_fn())
        self.journal.set_entry_index(index_at_fill)
        engine.confirm_fill(index_price=index_at_fill)
        return self.position

    def _exit(self, signal, engine):
        position = self.position
        if position is None:
            raise RuntimeError('Exit without an owned position')
        contract = position.contract
        spot = fresh_price(self.kite, 'NSE:NIFTY 50', self.now_fn())
        if not position.paper:
            assert_no_contract_orders(self.kite, contract)
            matching = [p for p in self.kite.positions().get('net', [])
                        if p.get('exchange') == 'NFO' and p.get('tradingsymbol') == contract['tradingsymbol']]
            if (sum(int(p.get('quantity') or 0) for p in matching) != position.quantity
                    or sum(int(p.get('quantity') or 0) for p in matching
                           if p.get('product') == self.kite.PRODUCT_MIS) != position.quantity):
                raise UncertainOrder('Owned option quantity/product changed; automatic exit forbidden')
        self.journal.exit_intent(signal, spot, paper=position.paper)
        if position.paper:
            order_id = None
            premium = fresh_price(self.kite, 'NFO:'+contract['tradingsymbol'], self.now_fn())
        else:
            order_id, premium = confirm_market_order(
                self.kite, contract, self.kite.TRANSACTION_TYPE_SELL,
                position.quantity, self.journal, exit_order=True)
        self.journal.exit_filled(order_id, premium)
        engine.confirm_fill()
        self.position = None
