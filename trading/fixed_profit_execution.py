"""Execution plumbing for user-operated fixed-profit runner; defaults paper."""
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime
import time

from market.kite_orders import place_protected_market_order
from strategies.nifty_opening_range import current_atm_option
from trading.reference_live_execution import fresh_price, FINAL, UncertainOrder, contract_quantity, assert_no_contract_orders

TAG='FIXPROFITFILTER1'


def select_contract(kite,instruments,today,side,spot):
    contract=current_atm_option(instruments,today,side,spot)
    if int(contract.get('lot_size') or 0)<1:raise ValueError('Invalid lot size')
    assert_no_contract_orders(kite,contract)
    if contract_quantity(kite,contract)!=0:
        raise RuntimeError('ATM contract already occupied; no alternate strike or merging')
    return contract


def confirmed_order(kite,contract,side,quantity,journal,*,attempts=20,sleep_fn=time.sleep):
    # Submit exactly once. Any ambiguity blocks restart and requires reconciliation.
    journal.event('ORDER_INTENT',side=side,symbol=contract['tradingsymbol'],quantity=quantity)
    try:
        order_id=place_protected_market_order(kite,variety=kite.VARIETY_REGULAR,
                    exchange='NFO',tradingsymbol=contract['tradingsymbol'],transaction_type=side,
                    quantity=quantity,product=kite.PRODUCT_MIS,order_type=kite.ORDER_TYPE_MARKET,
                    validity=kite.VALIDITY_DAY,tag=TAG)
    except Exception as exc:
        raise UncertainOrder('Order submission outcome unconfirmed; no automatic retry') from exc
    journal.event('ORDER_SUBMITTED',order_id=str(order_id))
    for _ in range(attempts):
        try:history=kite.order_history(order_id)
        except Exception as exc:raise UncertainOrder('Order status unavailable') from exc
        if not history:raise UncertainOrder('Missing order history')
        state=history[-1]
        if state['status']=='COMPLETE':
            price=Decimal(str(state.get('average_price') or 0))
            if int(state.get('filled_quantity') or 0)!=quantity or not price.is_finite() or price<=0:
                raise UncertainOrder('Unexpected broker fill; reconcile')
            return str(order_id),price
        if state['status'] in FINAL:
            raise UncertainOrder('Order rejected/cancelled; reconcile before any retry')
        sleep_fn(1)
    raise UncertainOrder('Order still unresolved; no automatic retry')


@dataclass
class Position:
    contract:dict
    quantity:int
    premium:Decimal


class FixedProfitExecutor:
    def __init__(self,kite,instruments,journal,*,live=False,now_fn):
        self.kite=kite;self.instruments=instruments;self.journal=journal
        self.live=live;self.now_fn=now_fn;self.position=None

    def enter(self,session):
        now=self.now_fn();spot=fresh_price(self.kite,'NSE:NIFTY 50',now)
        if not session.qualify_entry(spot,observed_at=now):
            self.journal.event('ENTRY_SKIPPED',reason=session.filter_decision.reason if session.filter_decision else 'stale_or_invalid_entry')
            return False
        engine=session.engine;signal=engine.pending
        contract=select_contract(self.kite,self.instruments,now.date(),signal.action[-2:],spot)
        quantity=int(contract['lot_size'])
        premium=fresh_price(self.kite,'NFO:'+contract['tradingsymbol'],self.now_fn())
        # Validate original qualification again immediately before recording intent.
        spot=fresh_price(self.kite,'NSE:NIFTY 50',self.now_fn())
        if not session.qualify_entry(spot,observed_at=self.now_fn()):return False
        self.journal.event('ENTRY_INTENT',side=signal.action[-2:],symbol=contract['tradingsymbol'],
                           quantity=quantity,expiry=str(contract['expiry']),strike=str(contract['strike']),index=str(spot),reference_high=str(engine.reference.high),
                           reference_low=str(engine.reference.low),atr=str(session.filter_decision.atr),
                           crossings=session.filter_decision.crossings)
        order_id=None
        if self.live:
            assert_no_contract_orders(self.kite,contract)
            if contract_quantity(self.kite,contract)!=0:raise UncertainOrder('Selected contract became occupied')
            order_id,premium=confirmed_order(self.kite,contract,self.kite.TRANSACTION_TYPE_BUY,quantity,self.journal)
        self.position=Position(contract,quantity,premium)
        # Adopt the confirmed position at its pre-order index sample. Never reject
        # an already-filled broker order because a later index quote moved.
        direction=1 if signal.action.endswith('CE') else -1
        engine.position=signal.action[-2:];engine.entries+=1
        engine.stop=spot-direction*15;engine.target=spot+direction*30
        engine.pending=engine.entry_setup=None
        self.journal.event('ENTRY_FILLED',order_id=order_id,premium=str(premium),index=str(spot),
                           stop=str(engine.stop),target=str(engine.target),mode='live' if self.live else 'paper')
        return True

    def exit(self,signal,engine):
        position=self.position
        if position is None:raise RuntimeError('No owned position')
        now=self.now_fn();spot=fresh_price(self.kite,'NSE:NIFTY 50',now)
        contract=position.contract
        if self.live:
            assert_no_contract_orders(self.kite,contract)
            matching=[p for p in self.kite.positions().get('net',[]) if p.get('exchange')=='NFO' and p.get('tradingsymbol')==contract['tradingsymbol']]
            if (sum(int(p.get('quantity') or 0) for p in matching)!=position.quantity or
                sum(int(p.get('quantity') or 0) for p in matching if p.get('product')==self.kite.PRODUCT_MIS)!=position.quantity):
                raise UncertainOrder('Owned quantity/product changed; reconcile')
        self.journal.event('EXIT_INTENT',reason=signal.reason,index=str(spot))
        if self.live:
            order_id,premium=confirmed_order(self.kite,contract,self.kite.TRANSACTION_TYPE_SELL,position.quantity,self.journal)
        else:
            order_id=None;premium=fresh_price(self.kite,'NFO:'+contract['tradingsymbol'],now)
        self.journal.event('EXIT_FILLED',order_id=order_id,premium=str(premium),
                           gross_option_pnl=str((premium-position.premium)*position.quantity))
        engine.confirm_exit();self.position=None
