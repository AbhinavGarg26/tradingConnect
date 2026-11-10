"""NIFTY noise-area option runner. Paper by default; protected live is explicit.

Live mode requires enabled strategy risk settings and a fresh independent
guardian heartbeat. Unknown broker outcomes are never retried automatically.
"""
import argparse
import logging
import time as sleeping
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select, text

from nifty_15m_env import load_nifty_environment
load_nifty_environment()

from market.database_candles import MinuteFeed, _local
from market.kite_orders import place_protected_market_order
from strategies.nifty_noise_area import KEY, VERSION, LOOKBACK, NoiseAreaMomentum, NoiseSignal
from strategies.nifty_opening_range import IST, current_atm_option
from trading.reference_live_execution import fresh_price, contract_quantity, assert_no_contract_orders, UncertainOrder

log = logging.getLogger(__name__)
TAG = "NIFTYNOISEV1"


def startup_allowed(now):
    return now.weekday() < 5 and time(9, 15) <= now.time() < time(9, 40)


def load_history(factory, today):
    """Load fourteen complete prior sessions without filling data gaps."""
    start = datetime.combine(today - timedelta(days=45), time(3, 45))
    end = datetime.combine(today, time(3, 45))
    with factory() as db:
        rows = list(db.execute(text("""SELECT captured_at,open_price,high_price,low_price,close_price
            FROM market_snapshots WHERE symbol='NIFTY 50' AND timeframe='1m'
            AND captured_at>=:start AND captured_at<:end ORDER BY captured_at"""),
            {"start": start, "end": end}))
    sessions = defaultdict(list)
    for row in rows:
        stamp = _local(row[0])
        sessions[stamp.date()].append((stamp, *(Decimal(str(v)) for v in row[1:])))
    complete = []
    for day, bars in sorted(sessions.items()):
        opening = datetime.combine(day, time(9, 15), IST)
        if (len(bars) == 375 and all(bar[0] == opening + timedelta(minutes=i)
                                     for i, bar in enumerate(bars))):
            complete.append((day, bars))
    if len(complete) < LOOKBACK:
        raise RuntimeError("Need fourteen complete prior NIFTY sessions in market_snapshots")
    selected = complete[-LOOKBACK:]
    history = defaultdict(list)
    for _, bars in selected:
        session_open = bars[0][1]
        for bar in bars:
            clock = NoiseAreaMomentum.decision_clock(bar[0])
            if clock:
                history[clock].append(abs(bar[4] / session_open - 1))
    return history, selected[-1][1][-1][4]


def confirmed_order(kite, contract, transaction, quantity, journal, *, sleep_fn=sleeping.sleep):
    journal.event("ORDER_INTENT", transaction=transaction,
                  symbol=contract["tradingsymbol"], quantity=quantity)
    try:
        order_id = place_protected_market_order(
            kite, variety=kite.VARIETY_REGULAR, exchange="NFO",
            tradingsymbol=contract["tradingsymbol"], transaction_type=transaction,
            quantity=quantity, product=kite.PRODUCT_MIS, order_type=kite.ORDER_TYPE_MARKET,
            validity=kite.VALIDITY_DAY, tag=TAG)
    except Exception as exc:
        raise UncertainOrder("Order submission outcome is unknown; reconcile manually") from exc
    journal.event("ORDER_SUBMITTED", order_id=str(order_id), transaction=transaction)
    for _ in range(20):
        history = kite.order_history(order_id)
        if not history:
            raise UncertainOrder("Broker returned no order history")
        state = history[-1]
        if state["status"] == "COMPLETE":
            price = Decimal(str(state.get("average_price") or 0))
            if price <= 0 or int(state.get("filled_quantity") or 0) != quantity:
                raise UncertainOrder("Unexpected broker fill; reconcile manually")
            return str(order_id), price
        if state["status"] in {"REJECTED", "CANCELLED"}:
            raise UncertainOrder(f"Order reached {state['status']}; reconcile before retry")
        sleep_fn(1)
    raise UncertainOrder("Order remains unresolved; no automatic retry")


class Journal:
    def __init__(self, factory, user_id, today, live):
        from trading.strategy_records import StrategyRun, StrategyTrade
        self.factory, self.user_id, self.today = factory, user_id, today
        self.mode = "live" if live else "paper"; self.trade_id = None
        with factory() as db:
            unresolved = db.scalar(select(StrategyTrade.id).where(
                StrategyTrade.user_id == user_id, StrategyTrade.strategy_key == KEY,
                StrategyTrade.status != "closed").limit(1))
            prior = db.scalar(select(StrategyRun.id).where(
                StrategyRun.user_id == user_id, StrategyRun.strategy_key == KEY,
                StrategyRun.mode == self.mode,
                StrategyRun.configuration["session_date"].as_string() == str(today)).limit(1))
            if unresolved or prior:
                raise RuntimeError("Prior run or unresolved noise-area position requires reconciliation")
            now = datetime.now(timezone.utc)
            run = StrategyRun(user_id=user_id, strategy_key=KEY, strategy_version=VERSION,
                mode=self.mode, status="running", started_at=now, heartbeat_at=now,
                configuration={"session_date": str(today), "lookback": LOOKBACK,
                               "multiplier": "1", "activation": "explicit_live" if live else "paper"})
            db.add(run); db.flush(); self.run_id = run.id

    def event(self, action, **details):
        from trading.strategy_records import StrategyEvent, StrategyTrade
        now = datetime.now(timezone.utc)
        with self.factory() as db:
            trade = db.get(StrategyTrade, self.trade_id, with_for_update=True) if self.trade_id else None
            if trade:
                if action == "ORDER_SUBMITTED":
                    if details["transaction"] == "BUY": trade.entry_order_id = details["order_id"]
                    else: trade.exit_order_id = details["order_id"]
                elif action == "PROTECTION_SUBMITTED":
                    trade.protection_order_id = details["order_id"]
                    trade.protection_trigger = Decimal(details["trigger"])
                    trade.protection_limit = Decimal(details["limit"])
                elif action == "RECONCILE_REQUIRED": trade.status = "reconcile"
                trade.updated_at = now
            db.add(StrategyEvent(strategy_run_id=self.run_id, strategy_trade_id=self.trade_id,
                occurred_at=now, action=action, reason=str(details.get("reason", action.lower())),
                details={k: str(v) for k, v in details.items()}))

    def reserve_entry(self, signal, contract, quantity):
        from trading.strategy_records import StrategyTrade
        with self.factory() as db:
            existing = list(db.scalars(select(StrategyTrade.id).where(
                StrategyTrade.user_id == self.user_id, StrategyTrade.strategy_key == KEY,
                StrategyTrade.mode == self.mode, StrategyTrade.trading_date == self.today)))
            trade = StrategyTrade(strategy_run_id=self.run_id, user_id=self.user_id,
                strategy_key=KEY, strategy_version=VERSION, mode=self.mode,
                trading_date=self.today, trade_number=len(existing) + 1,
                status="entry_pending", side=signal.action[-2:], option_symbol=contract["tradingsymbol"],
                option_expiry=contract["expiry"], option_strike=contract["strike"], lot_count=1,
                quantity=quantity, entry_signal_time=signal.time, entry_reason=signal.reason,
                entry_index=signal.index_price, reference_high=signal.upper,
                reference_low=signal.lower, initial_stop=None, current_stop=None, target=None)
            db.add(trade); db.flush(); self.trade_id = trade.id
        self.event("ENTRY_RESERVED", side=signal.action[-2:], symbol=contract["tradingsymbol"], quantity=quantity)

    def entry_filled(self, order_id, premium):
        from trading.strategy_records import StrategyTrade
        with self.factory() as db:
            trade = db.get(StrategyTrade, self.trade_id, with_for_update=True)
            if trade.status != "entry_pending": raise RuntimeError("Entry reservation changed")
            trade.status = "open"; trade.entry_order_id = order_id; trade.entry_premium = premium
            trade.entry_confirmed_at = datetime.now(timezone.utc)

    def exit_intent(self, signal):
        from trading.strategy_records import StrategyTrade
        with self.factory() as db:
            trade = db.get(StrategyTrade, self.trade_id, with_for_update=True)
            if trade.status != "open": raise RuntimeError("Position ownership changed")
            trade.status = "exit_pending"; trade.exit_signal_time = signal.time
            trade.exit_reason = signal.reason; trade.exit_index = signal.index_price

    def exit_filled(self, order_id, premium):
        from trading.strategy_records import StrategyTrade
        with self.factory() as db:
            trade = db.get(StrategyTrade, self.trade_id, with_for_update=True)
            if trade.status != "exit_pending": raise RuntimeError("Exit reservation changed")
            trade.status = "closed"; trade.exit_order_id = order_id; trade.exit_premium = premium
            trade.exit_confirmed_at = datetime.now(timezone.utc)
            trade.gross_option_pnl = (premium - trade.entry_premium) * trade.quantity
            direction = Decimal(1) if trade.side == "CE" else Decimal(-1)
            trade.index_points = direction * (trade.exit_index - trade.entry_index)
        self.trade_id = None

    def heartbeat(self):
        from trading.strategy_records import StrategyRun
        with self.factory() as db:
            run = db.get(StrategyRun, self.run_id); run.heartbeat_at = run.updated_at = datetime.now(timezone.utc)

    def finish(self, status, error=None):
        from trading.strategy_records import StrategyRun
        with self.factory() as db:
            run = db.get(StrategyRun, self.run_id); run.status = status; run.error_message = error
            run.ended_at = run.heartbeat_at = run.updated_at = datetime.now(timezone.utc)


class Executor:
    def __init__(self, kite, instruments, journal, user_id, live):
        self.kite, self.instruments, self.journal = kite, instruments, journal
        self.user_id, self.live, self.position = user_id, live, None

    def enter(self, signal, engine):
        spot = fresh_price(self.kite, "NSE:NIFTY 50", datetime.now(IST))
        contract = current_atm_option(self.instruments, datetime.now(IST).date(), signal.action[-2:], spot)
        quantity = int(contract.get("lot_size") or 0)
        if quantity < 1: raise RuntimeError("Invalid option lot size")
        assert_no_contract_orders(self.kite, contract)
        if contract_quantity(self.kite, contract) != 0: raise RuntimeError("Selected option contract is occupied")
        premium = fresh_price(self.kite, "NFO:" + contract["tradingsymbol"], datetime.now(IST))
        limits = None
        if self.live:
            from trading.strategy_risk import check_new_entry
            limits = check_new_entry(self.user_id, KEY, datetime.now(IST).date(), premium, quantity)
        self.journal.reserve_entry(signal, contract, quantity)
        order_id = None
        if self.live:
            order_id, premium = confirmed_order(self.kite, contract, self.kite.TRANSACTION_TYPE_BUY,
                                                quantity, self.journal)
        self.journal.entry_filled(order_id, premium)
        self.position = {"contract": contract, "quantity": quantity, "premium": premium,
                         "protection": None}
        engine.confirm_entry()
        if self.live:
            from trading.strategy_risk import stop_limit_prices
            protection = None
            try:
                trigger, limit = stop_limit_prices(premium, limits.risk_per_trade, quantity,
                                                    Decimal(str(contract["tick_size"])), limits.stop_limit_buffer_pct)
                protection = self.kite.place_order(variety=self.kite.VARIETY_REGULAR, exchange="NFO",
                    tradingsymbol=contract["tradingsymbol"], transaction_type=self.kite.TRANSACTION_TYPE_SELL,
                    quantity=quantity, product=self.kite.PRODUCT_MIS, order_type=self.kite.ORDER_TYPE_SL,
                    trigger_price=float(trigger), price=float(limit), validity=self.kite.VALIDITY_DAY,
                    tag="NIFTYNOISESL")
                status = self.kite.order_history(protection)[-1]["status"]
                if status not in {"TRIGGER PENDING", "OPEN"}:
                    raise RuntimeError(f"Protection order reached {status}")
                self.position["protection"] = str(protection)
                self.journal.event("PROTECTION_SUBMITTED", order_id=str(protection), trigger=trigger, limit=limit)
                if premium * quantity > limits.amount_allocated:
                    raise RuntimeError("Actual fill exceeded strategy allocation")
            except Exception as exc:
                if protection is not None:
                    self.kite.cancel_order(variety=self.kite.VARIETY_REGULAR, order_id=protection)
                    cancelled = self.kite.order_history(protection)[-1]["status"]
                    if cancelled != "CANCELLED":
                        raise UncertainOrder(
                            "Protection failure and cancellation is unconfirmed; position remains protected, reconcile") from exc
                emergency = NoiseSignal(f"EXIT_{engine.position}", datetime.now(IST), spot,
                                        "protection_failed")
                self.journal.exit_intent(emergency)
                emergency_id, emergency_price = confirmed_order(
                    self.kite, contract, self.kite.TRANSACTION_TYPE_SELL, quantity, self.journal)
                self.journal.exit_filled(emergency_id, emergency_price)
                self.position = None; engine.position = engine.pending = None
                raise RuntimeError("Protection failed after buy; emergency market exit confirmed") from exc

    def exit(self, signal, engine):
        if not self.position: raise RuntimeError("No owned strategy position")
        contract = self.position["contract"]; quantity = self.position["quantity"]
        self.journal.exit_intent(signal)
        if self.live:
            if contract_quantity(self.kite, contract) != quantity:
                raise UncertainOrder("Broker position differs from strategy ownership")
            protection = self.position["protection"]
            if protection:
                self.kite.cancel_order(variety=self.kite.VARIETY_REGULAR, order_id=protection)
                state = self.kite.order_history(protection)[-1]["status"]
                if state != "CANCELLED": raise UncertainOrder("Protection cancellation unconfirmed")
            order_id, premium = confirmed_order(self.kite, contract, self.kite.TRANSACTION_TYPE_SELL,
                                                quantity, self.journal)
        else:
            order_id = None
            premium = fresh_price(self.kite, "NFO:" + contract["tradingsymbol"], datetime.now(IST))
        self.journal.exit_filled(order_id, premium); self.position = None; engine.confirm_exit()


def run_session(kite, user_id, factory, *, live=False, poll_seconds=3, now_fn=None, sleep_fn=sleeping.sleep):
    now_fn = now_fn or (lambda: datetime.now(IST)); today = now_fn().date()
    history, previous_close = load_history(factory, today)
    engine = NoiseAreaMomentum(history, previous_close)
    journal = Journal(factory, user_id, today, live)
    executor = Executor(kite, kite.instruments("NFO"), journal, user_id, live)
    feed = MinuteFeed(kite, factory=factory); last = None
    try:
        while True:
            now = now_fn()
            if now.date() != today: raise RuntimeError("Session changed with runner active")
            for row in feed.read(now, after=last):
                stamp = row["date"]; last = stamp
                if engine.session_open is None:
                    if stamp.time() != time(9, 15): raise RuntimeError("Missing current session opening minute")
                    engine.start_session(stamp, row["open"])
                signal = engine.on_minute(stamp, row["close"])
                if signal and signal.action.startswith("BUY_"): executor.enter(signal, engine)
                elif signal and signal.action.startswith("EXIT_"): executor.exit(signal, engine)
            if now.time() >= time(15, 16) and not executor.position:
                journal.finish("completed"); return
            journal.heartbeat(); sleep_fn(poll_seconds)
    except BaseException as exc:
        journal.event("RECONCILE_REQUIRED", reason=type(exc).__name__)
        journal.finish("stopped" if isinstance(exc, KeyboardInterrupt) else "failed", str(exc)[:2000])
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=3)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    from trading.database import get_db
    from trading.user_token import fetch_user_token
    from market.trading_calendar import is_nse_trading_day
    now = datetime.now(IST)
    kite, user_id = fetch_user_token(log, user_id=args.user_id); kite.profile()
    with get_db() as db: trading_day = is_nse_trading_day(now.date(), db)
    history, previous = load_history(get_db, now.date())
    if args.check:
        print({"strategy": KEY, "version": VERSION, "mode": "live" if args.live else "paper",
               "orders": "DISABLED", "history_sessions": min(map(len, history.values())),
               "previous_close": str(previous), "trading_day": trading_day})
        return
    if not trading_day or not startup_allowed(now):
        print("No session started: schedule before 09:40 IST on an NSE trading day."); return
    run_session(kite, user_id, get_db, live=args.live, poll_seconds=args.poll_seconds)


if __name__ == "__main__":
    main()
