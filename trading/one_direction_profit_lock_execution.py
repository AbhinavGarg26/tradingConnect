"""Paper and protected-live execution for One Direction Profit Lock."""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from strategies.nifty_opening_range import current_atm_option
from trading.one_direction_profit_lock_journal import KEY
from trading.reference_live_execution import (
    FINAL, UncertainOrder, assert_no_contract_orders, confirm_market_order,
    contract_quantity, fresh_price,
)
from trading.strategy_risk import check_new_entry, stop_limit_prices
from trading.trade_execution_lock import trade_execution_lock

ENTRY_TAG = "ODPROFITV21"
STOP_TAG = "ODPROFITSL"


def option_book(kite, contract, now, *, max_age_seconds=20):
    key = "NFO:" + contract["tradingsymbol"]
    quote = kite.quote([key]).get(key) or {}
    timestamp = quote.get("timestamp")
    if not isinstance(timestamp, datetime): raise RuntimeError("Option quote timestamp missing")
    if timestamp.tzinfo is None: timestamp = timestamp.replace(tzinfo=now.tzinfo)
    if not -5 <= (now - timestamp).total_seconds() <= max_age_seconds:
        raise RuntimeError("Option quote is stale")
    depth = quote.get("depth") or {}
    buys, sells = depth.get("buy") or [], depth.get("sell") or []
    bid = Decimal(str(buys[0].get("price") or 0)) if buys else Decimal(0)
    ask = Decimal(str(sells[0].get("price") or 0)) if sells else Decimal(0)
    if bid <= 0 or ask <= 0 or ask < bid: raise RuntimeError("Executable option bid/ask unavailable")
    return {"bid": bid, "ask": ask, "spread": ask - bid, "timestamp": timestamp}


@dataclass
class ProfitPosition:
    contract: dict
    quantity: int
    entry: Decimal
    peak: Decimal
    stop: Decimal
    paper: bool


def place_profit_stop(kite, contract, quantity, trigger, limit, journal):
    order_id = kite.place_order(
        variety=kite.VARIETY_REGULAR, exchange="NFO",
        tradingsymbol=contract["tradingsymbol"], transaction_type=kite.TRANSACTION_TYPE_SELL,
        quantity=quantity, product=kite.PRODUCT_MIS, order_type=kite.ORDER_TYPE_SL,
        trigger_price=float(trigger), price=float(limit), validity=kite.VALIDITY_DAY,
        tag=STOP_TAG,
    )
    journal.protection_submitted(order_id, trigger, limit)
    history = kite.order_history(order_id)
    state = history[-1] if history else {}
    if state.get("status") not in {"TRIGGER PENDING", "OPEN"} or int(state.get("filled_quantity") or 0):
        raise UncertainOrder("Profit Lock protective stop was not confirmed")
    return str(order_id)


class ProfitLockExecutor:
    def __init__(self, kite, instruments, journal, user_id, *, live, now_fn):
        self.kite, self.instruments, self.journal, self.user_id = kite, instruments, journal, user_id
        self.live, self.now_fn, self.position = live, now_fn, None

    def restore(self, runtime):
        trade = self.journal.trade_snapshot()
        if trade is None: return None
        if trade.status == "reconcile": raise UncertainOrder("Persisted Profit Lock trade requires reconciliation")
        contract = next((item for item in self.instruments
                         if item.get("tradingsymbol") == trade.option_symbol), None)
        if contract is None: raise UncertainOrder("Persisted option contract is no longer listed")
        if trade.status == "exit_pending":
            if not self.live: raise UncertainOrder("Paper exit was interrupted; reconcile recorded quote")
            return None  # Guardian owns live pending-exit recovery.
        if trade.status not in {"open", "entry_pending"}:
            raise UncertainOrder(f"Unexpected persisted trade status {trade.status}")
        if trade.status == "entry_pending":
            raise UncertainOrder("Interrupted entry requires broker reconciliation")
        if self.live:
            quantity = contract_quantity(self.kite, contract)
            if quantity != trade.quantity: raise UncertainOrder("Broker quantity differs from durable journal")
            history = self.kite.order_history(trade.protection_order_id) if trade.protection_order_id else []
            state = history[-1] if history else {}
            if state.get("status") not in {"TRIGGER PENDING", "OPEN"}:
                raise UncertainOrder("Durable trade has no active broker protection")
        self.position = ProfitPosition(
            contract, trade.quantity, Decimal(trade.entry_premium),
            Decimal(str(runtime.get("peak", trade.entry_premium))), Decimal(trade.current_stop),
            self.live is False,
        )
        return self.position

    def enter(self, side, snapshot, gate):
        if self.position: raise RuntimeError("Profit Lock already owns a position")
        now = self.now_fn()
        spot = fresh_price(self.kite, "NSE:NIFTY 50", now)
        contract = current_atm_option(self.instruments, now.date(), side, spot)
        quantity = int(contract.get("lot_size") or 0)
        if quantity < 1: raise RuntimeError("Invalid exchange lot size")
        assert_no_contract_orders(self.kite, contract)
        if contract_quantity(self.kite, contract): raise UncertainOrder("Selected option contract is occupied")
        book = option_book(self.kite, contract, now)
        premium = book["ask"]
        self.journal.event("OPTION_BOOK", "entry_quote", details=book)
        limits = check_new_entry(self.user_id, KEY, now.date(), premium, quantity) if self.live else None
        initial_stop = (premium * Decimal("0.90")).quantize(Decimal("0.05"))
        self.journal.reserve_entry(side, snapshot, contract, quantity, premium)
        order_id = None
        if self.live:
            order_id, premium = confirm_market_order(
                self.kite, contract, self.kite.TRANSACTION_TYPE_BUY, quantity, self.journal,
                tag=ENTRY_TAG,
            )
            trigger, limit = stop_limit_prices(
                premium, limits.risk_per_trade, quantity, Decimal(str(contract["tick_size"])),
                limits.stop_limit_buffer_pct,
            )
            initial_stop = trigger
        self.journal.entry_filled(order_id, premium, initial_stop)
        self.position = ProfitPosition(contract, quantity, premium, premium, initial_stop, not self.live)
        gate.record_entry(side, snapshot)
        if self.live:
            try:
                with trade_execution_lock(self.journal.trade_id) as acquired:
                    if not acquired: raise UncertainOrder("Guardian owns post-fill reconciliation")
                    place_profit_stop(self.kite, contract, quantity, trigger, limit, self.journal)
                    if premium * quantity > limits.amount_allocated:
                        raise UncertainOrder("Confirmed premium exceeds allocated capital")
            except Exception as exc:
                from strategy_guardian import emergency_exit
                trade = self.journal.trade_snapshot()
                if trade and trade.status == "open": emergency_exit(self.kite, trade, "broker_protection_failed")
                raise UncertainOrder("Protection failed; emergency handling attempted") from exc
        return self.position

    def exit(self, reason, snapshot):
        position = self.position
        if position is None: raise RuntimeError("No Profit Lock position")
        self.journal.exit_intent(reason, snapshot)
        if position.paper:
            order_id = None
            book = option_book(self.kite, position.contract, self.now_fn())
            premium = book["bid"]
            self.journal.event("OPTION_BOOK", "exit_quote", details=book)
        else:
            from strategy_guardian import cancel_protection, finish_exit
            with trade_execution_lock(self.journal.trade_id) as acquired:
                if not acquired: return None
                trade = self.journal.trade_snapshot()
                completed = cancel_protection(self.kite, trade)
                if completed is not None:
                    finish_exit(trade.id, trade.protection_order_id, completed["average_price"], "broker_stop")
                    premium = Decimal(str(completed["average_price"])); self.position = None
                    return (premium - position.entry) * position.quantity
                assert_no_contract_orders(self.kite, position.contract)
                if contract_quantity(self.kite, position.contract) != position.quantity:
                    raise UncertainOrder("Position changed after stop cancellation")
                order_id, premium = confirm_market_order(
                    self.kite, position.contract, self.kite.TRANSACTION_TYPE_SELL,
                    position.quantity, self.journal, exit_order=True, tag=ENTRY_TAG,
                )
        pnl = self.journal.exit_filled(order_id, premium)
        self.position = None
        return pnl

    def synchronize(self):
        if not self.live or self.position is None: return False
        trade = self.journal.trade_snapshot()
        if trade.status == "closed": self.position = None; self.journal.trade_id = None; return False
        if trade.status == "exit_pending": return True
        if trade.status != "open": raise UncertainOrder(f"Trade is {trade.status}; reconcile")
        return False

    def trail_stop(self, stop, peak):
        if self.position is None or stop <= self.position.stop: return
        if not self.live:
            self.journal.update_profit_stop(stop, peak); self.position.stop = stop; return
        from trading.strategy_risk import load_limits
        limits = load_limits(self.user_id, KEY)
        tick = Decimal(str(self.position.contract["tick_size"]))
        trigger = (Decimal(stop) / tick).to_integral_value() * tick
        limit = ((trigger * (Decimal(1) - limits.stop_limit_buffer_pct / Decimal(100))) / tick).to_integral_value() * tick
        trade = self.journal.trade_snapshot()
        if not trade.protection_order_id or limit <= 0 or limit >= trigger:
            raise UncertainOrder("Cannot safely trail broker protection")
        with trade_execution_lock(trade.id) as acquired:
            if not acquired: return
            self.kite.modify_order(
                variety=self.kite.VARIETY_REGULAR, order_id=trade.protection_order_id,
                order_type=self.kite.ORDER_TYPE_SL, trigger_price=float(trigger), price=float(limit),
            )
            history = self.kite.order_history(trade.protection_order_id)
            state = history[-1] if history else {}
            if state.get("status") not in {"TRIGGER PENDING", "OPEN"}:
                raise UncertainOrder("Trailed broker stop was not confirmed")
            self.journal.protection_submitted(trade.protection_order_id, trigger, limit)
            self.journal.update_profit_stop(trigger, peak); self.position.stop = trigger
