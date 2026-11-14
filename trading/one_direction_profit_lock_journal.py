"""Durable journal and restart state for One Direction Profit Lock V2.1."""
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from trading.strategy_records import StrategyEvent, StrategyRun, StrategyTrade

KEY = "one_direction_profit_lock"
VERSION = "v2.1-paper"


class ProfitLockJournal:
    def __init__(self, factory, user_id, today, *, live=False):
        self.factory, self.user_id, self.today = factory, user_id, today
        self.mode = "live" if live else "paper"
        self.trade_id = None
        with factory() as db:
            unresolved = db.scalar(select(StrategyTrade).where(
                StrategyTrade.user_id == user_id,
                StrategyTrade.strategy_key == KEY,
                StrategyTrade.status.in_(["open", "entry_pending", "exit_pending", "reconcile"]),
            ).order_by(StrategyTrade.id.desc()).limit(1))
            running = db.scalar(select(StrategyRun).where(
                StrategyRun.user_id == user_id,
                StrategyRun.strategy_key == KEY,
                StrategyRun.mode == self.mode,
                StrategyRun.status.in_(["running", "stopped"]),
            ).order_by(StrategyRun.id.desc()).limit(1))
            if unresolved:
                if unresolved.trading_date != today or running is None or running.id != unresolved.strategy_run_id:
                    raise RuntimeError("Unresolved Profit Lock trade requires reconciliation")
                self.run_id, self.trade_id = running.id, unresolved.id
                running.status = "running"; running.ended_at = None
                running.heartbeat_at = running.updated_at = datetime.now(timezone.utc)
                self.restored_state = (running.configuration or {}).get("gate_state")
                self.restored_runtime = (running.configuration or {}).get("runtime") or {}
                return
            if running:
                config = running.configuration or {}
                if config.get("session_date") != str(today):
                    raise RuntimeError("Stale running Profit Lock session requires review")
                self.run_id = running.id; running.status = "running"; running.ended_at = None
                running.heartbeat_at = running.updated_at = datetime.now(timezone.utc)
                self.restored_state = config.get("gate_state")
                self.restored_runtime = config.get("runtime") or {}
                return
            now = datetime.now(timezone.utc)
            run = StrategyRun(
                user_id=user_id, strategy_key=KEY, strategy_version=VERSION,
                mode=self.mode, status="running", started_at=now, heartbeat_at=now,
                configuration={"session_date": str(today), "gate_state": None, "runtime": {},
                               "expiry": "nearest_unexpired", "strike": "atm",
                               "evidence": "live_option_quotes"},
            )
            db.add(run); db.flush(); self.run_id = run.id
            self.restored_state = None; self.restored_runtime = {}

    def event(self, action, reason, *, details=None, occurred_at=None):
        with self.factory() as db:
            db.add(StrategyEvent(
                strategy_run_id=self.run_id, strategy_trade_id=self.trade_id,
                occurred_at=occurred_at or datetime.now(timezone.utc), action=action,
                reason=reason, details={k: str(v) for k, v in (details or {}).items()},
            ))

    def save_state(self, gate_state, runtime=None):
        now = datetime.now(timezone.utc)
        with self.factory() as db:
            run = db.get(StrategyRun, self.run_id, with_for_update=True)
            config = dict(run.configuration or {})
            config["gate_state"] = gate_state
            if runtime is not None: config["runtime"] = runtime
            run.configuration = config; run.heartbeat_at = run.updated_at = now

    def reserve_entry(self, side, snapshot, contract, quantity, premium):
        with self.factory() as db:
            count = len(list(db.scalars(select(StrategyTrade.id).where(
                StrategyTrade.user_id == self.user_id, StrategyTrade.strategy_key == KEY,
                StrategyTrade.trading_date == self.today))))
            trade = StrategyTrade(
                strategy_run_id=self.run_id, user_id=self.user_id, strategy_key=KEY,
                strategy_version=VERSION, mode=self.mode, trading_date=self.today,
                trade_number=count + 1, status="entry_pending", side=side,
                option_symbol=contract["tradingsymbol"], option_expiry=contract["expiry"],
                option_strike=contract["strike"], lot_count=max(1, quantity // int(contract["lot_size"])),
                quantity=quantity, entry_signal_time=snapshot.observed_at,
                entry_reason="confirmed_market_flow", entry_index=snapshot.close,
                entry_premium=premium, initial_stop=None, current_stop=None, target=None,
            )
            db.add(trade); db.flush(); self.trade_id = trade.id
        self.event("ENTRY_RESERVED", "confirmed_market_flow",
                   details={"side": side, "symbol": contract["tradingsymbol"], "quantity": quantity})

    def order_submitted(self, order_id, *, exit_order):
        with self.factory() as db:
            trade = db.get(StrategyTrade, self.trade_id, with_for_update=True)
            if exit_order: trade.exit_order_id = str(order_id)
            else: trade.entry_order_id = str(order_id)
            trade.updated_at = datetime.now(timezone.utc)
        self.event("ORDER_SUBMITTED", "exit" if exit_order else "entry", details={"order_id": order_id})

    def entry_filled(self, order_id, premium, stop):
        now = datetime.now(timezone.utc)
        with self.factory() as db:
            trade = db.get(StrategyTrade, self.trade_id, with_for_update=True)
            if trade.status != "entry_pending": raise RuntimeError("Entry ownership changed")
            trade.entry_order_id = str(order_id) if order_id else None
            trade.entry_premium = premium; trade.initial_stop = trade.current_stop = stop
            trade.entry_confirmed_at = now; trade.status = "open"; trade.updated_at = now
        self.event("ENTRY_FILLED", "broker" if self.mode == "live" else "paper_quote",
                   details={"premium": premium, "stop": stop})

    def protection_submitted(self, order_id, trigger, limit):
        with self.factory() as db:
            trade = db.get(StrategyTrade, self.trade_id, with_for_update=True)
            trade.protection_order_id = str(order_id); trade.protection_trigger = trigger
            trade.protection_limit = limit; trade.updated_at = datetime.now(timezone.utc)
        self.event("PROTECTION_SUBMITTED", "broker_stop_limit",
                   details={"order_id": order_id, "trigger": trigger, "limit": limit})

    def update_profit_stop(self, stop, peak):
        with self.factory() as db:
            trade = db.get(StrategyTrade, self.trade_id, with_for_update=True)
            if trade.status != "open": raise RuntimeError("Trade changed while trailing stop")
            if trade.current_stop is None or stop > trade.current_stop:
                trade.current_stop = stop; trade.updated_at = datetime.now(timezone.utc)
        self.event("PROFIT_LOCK_UPDATED", "premium_peak",
                   details={"stop": stop, "peak": peak})

    def exit_intent(self, reason, snapshot):
        with self.factory() as db:
            trade = db.get(StrategyTrade, self.trade_id, with_for_update=True)
            if trade.status != "open": raise RuntimeError("Exit ownership changed")
            trade.status = "exit_pending"; trade.exit_reason = reason
            trade.exit_signal_time = snapshot.observed_at; trade.exit_index = snapshot.close
            direction = Decimal(1) if trade.side == "CE" else Decimal(-1)
            trade.index_points = direction * (snapshot.close - trade.entry_index)
            trade.updated_at = datetime.now(timezone.utc)
        self.event(f"EXIT_{trade.side}", reason, occurred_at=snapshot.observed_at)

    def exit_filled(self, order_id, premium):
        now = datetime.now(timezone.utc)
        with self.factory() as db:
            trade = db.get(StrategyTrade, self.trade_id, with_for_update=True)
            if trade.status != "exit_pending": raise RuntimeError("Exit ownership changed")
            trade.exit_order_id = str(order_id) if order_id else None
            trade.exit_premium = premium; trade.exit_confirmed_at = now
            trade.gross_option_pnl = (premium - trade.entry_premium) * trade.quantity
            trade.status = "closed"; trade.updated_at = now
            pnl = trade.gross_option_pnl
        self.trade_id = None
        return pnl

    def trade_snapshot(self):
        if self.trade_id is None: return None
        with self.factory() as db:
            trade = db.get(StrategyTrade, self.trade_id)
            db.expunge(trade); return trade

    def uncertain(self, message):
        if self.trade_id is None: return
        with self.factory() as db:
            trade = db.get(StrategyTrade, self.trade_id, with_for_update=True)
            if trade.status != "closed":
                trade.status = "reconcile"; trade.error_message = str(message)[:2000]
                trade.updated_at = datetime.now(timezone.utc)

    def finish(self, status, error=None):
        now = datetime.now(timezone.utc)
        with self.factory() as db:
            run = db.get(StrategyRun, self.run_id, with_for_update=True)
            run.status = status; run.error_message = str(error)[:2000] if error else None
            run.ended_at = run.heartbeat_at = run.updated_at = now
