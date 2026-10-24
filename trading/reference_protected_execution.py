"""Risk-controlled reference execution. This module does not activate services."""
import time
from decimal import Decimal

from trading.reference_live_execution import ReferenceExecutor, UncertainOrder, confirm_market_order, fresh_price, FINAL
from trading.reference_live_records import KEY
from trading.strategy_risk import check_new_entry, stop_limit_prices
from trading.trade_execution_lock import trade_execution_lock


def place_reference_stop(kite, contract, quantity, premium, limits, journal):
    trigger, limit = stop_limit_prices(premium,limits.risk_per_trade,quantity,
                                      Decimal(str(contract['tick_size'])),limits.stop_limit_buffer_pct)
    order_id=kite.place_order(variety=kite.VARIETY_REGULAR,exchange='NFO',
        tradingsymbol=contract['tradingsymbol'],transaction_type=kite.TRANSACTION_TYPE_SELL,
        quantity=quantity,product=kite.PRODUCT_MIS,order_type=kite.ORDER_TYPE_SL,
        trigger_price=float(trigger),price=float(limit),validity=kite.VALIDITY_DAY,
        tag='NIFTY30STOP')
    journal.protection_submitted(order_id,trigger,limit)
    for _ in range(10):
        history=kite.order_history(order_id)
        if not history:
            raise UncertainOrder('Protective stop has no broker history')
        state=history[-1]
        if int(state.get('filled_quantity') or 0):
            raise UncertainOrder('Protective stop filled during installation; reconcile')
        if state['status'] in {'TRIGGER PENDING','OPEN'}:
            return str(order_id)
        if state['status'] in FINAL:
            raise UncertainOrder(f"Protective stop reached {state['status']}")
        time.sleep(0.5)
    raise UncertainOrder('Protective stop not confirmed; no resubmission')


class ProtectedReferenceExecutor(ReferenceExecutor):
    def _before_entry(self, contract, premium, quantity):
        # Reads live_enabled, allocation, daily loss and <=20s guardian heartbeat.
        self.limits=check_new_entry(self.journal.user_id,KEY,self.now_fn().date(),premium,quantity)
        stop_limit_prices(premium,self.limits.risk_per_trade,quantity,
                          Decimal(str(contract['tick_size'])),self.limits.stop_limit_buffer_pct)

    def _after_live_fill(self, contract, premium, quantity):
        try:
            with trade_execution_lock(self.journal.trade_id) as acquired:
                if not acquired:
                    raise UncertainOrder('Guardian owns post-fill reconciliation')
                if self.journal.trade_snapshot().status!='open':
                    raise UncertainOrder('Trade changed before protective stop installation')
                place_reference_stop(self.kite,contract,quantity,premium,self.limits,self.journal)
                if premium*quantity>self.limits.amount_allocated:
                    raise UncertainOrder('Actual premium cost exceeded allocated capital')
        except Exception as exc:
            # A known live fill is already persisted. Never switch it to paper.
            # The shared exit routine checks pending orders/quantity before selling.
            from strategy_guardian import emergency_exit
            trade=self.journal.trade_snapshot()
            if trade.status=='open':
                emergency_exit(self.kite,trade,'broker_protection_failed')
            raise UncertainOrder('Post-fill protection failed; emergency handling attempted, reconcile') from exc

    def _forget_closed(self, engine):
        # No fill assumption is made: database closure was broker-confirmed.
        engine.position=engine.stop=engine.pending=None
        self.position=None
        self.journal.release_closed_trade()

    def synchronize(self, engine):
        """Return True while another owner is completing an exit."""
        if self.position is None or self.position.paper:
            return False
        trade=self.journal.trade_snapshot()
        if trade.status=='closed':
            self._forget_closed(engine)
            return False
        if trade.status=='exit_pending':
            return True
        if trade.status!='open':
            raise UncertainOrder(f'Reference trade is {trade.status}; reconcile before continuing')
        return False

    def _exit(self, signal, engine):
        if self.position is None:
            raise UncertainOrder('Exit without an owned reference position')
        if self.position.paper:
            return super()._exit(signal,engine)
        from strategy_guardian import cancel_protection, finish_exit
        with trade_execution_lock(self.journal.trade_id) as acquired:
            if not acquired:
                return  # Keep the signal pending; next loop synchronizes/retries.
            trade=self.journal.trade_snapshot()
            if trade.status=='closed':
                self._forget_closed(engine)
                return
            if trade.status=='exit_pending':
                return
            if trade.status!='open':
                raise UncertainOrder('Reference trade requires reconciliation')
            position=self.position
            contract=position.contract
            matching=[p for p in self.kite.positions().get('net',[])
                      if p.get('exchange')=='NFO' and p.get('tradingsymbol')==contract['tradingsymbol']]
            total=sum(int(p.get('quantity') or 0) for p in matching)
            owned=sum(int(p.get('quantity') or 0) for p in matching if p.get('product')==self.kite.PRODUCT_MIS)
            # A stop may have completed just before the position snapshot.
            if total!=position.quantity or owned!=position.quantity:
                if trade.protection_order_id:
                    history=self.kite.order_history(trade.protection_order_id)
                    state=history[-1] if history else {}
                    if state.get('status')=='COMPLETE' and int(state.get('filled_quantity') or 0)==position.quantity:
                        finish_exit(trade.id,trade.protection_order_id,state['average_price'],'broker_stop')
                        self._forget_closed(engine)
                        return
                raise UncertainOrder('Owned option quantity/product changed; no automatic sell')
            if any(o.get('exchange')=='NFO' and o.get('tradingsymbol')==contract['tradingsymbol']
                   and o.get('status') not in FINAL and str(o.get('order_id'))!=str(trade.protection_order_id)
                   for o in self.kite.orders()):
                raise UncertainOrder('Unrelated pending order on owned contract')
            spot=fresh_price(self.kite,'NSE:NIFTY 50',self.now_fn())
            # Reserve before touching the broker stop; guardian shares this lock.
            self.journal.exit_intent(signal,spot,paper=False)
            completed=cancel_protection(self.kite,trade)
            if completed is not None:
                finish_exit(trade.id,trade.protection_order_id,completed['average_price'],'broker_stop')
                self._forget_closed(engine)
                return
            # Recheck quantity and all pending orders after cancellation.
            from trading.reference_live_execution import contract_quantity, assert_no_contract_orders
            assert_no_contract_orders(self.kite,contract)
            if contract_quantity(self.kite,contract)!=position.quantity:
                raise UncertainOrder('Position changed after protective stop cancellation')
            order_id,premium=confirm_market_order(self.kite,contract,self.kite.TRANSACTION_TYPE_SELL,
                position.quantity,self.journal,exit_order=True)
            self.journal.exit_filled(order_id,premium)
            engine.confirm_fill()
            self.position=None
