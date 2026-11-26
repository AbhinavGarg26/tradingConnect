"""Frozen Groww ORB intraday option runner; paper by default, live is explicit.

Signals are generated exclusively from completed NIFTY one-minute candles.  A
live entry is one nearest-expiry ATM lot and is rejected unless the shared
risk/guardian gate is healthy; a broker-held option stop is installed before
the runner continues.  This replaces the old day-end replay only for intraday
execution -- the replay remains the independent paper-evidence source.
"""
from __future__ import annotations
import argparse
import logging
import time
from datetime import datetime, timedelta, timezone, time as clock_time
from decimal import Decimal
from zoneinfo import ZoneInfo

from market.kite_orders import place_protected_market_order
from strategies.groww_orb_intraday import GrowwOrbIntraday
from strategies.nifty_opening_range import current_atm_option
from trading.strategy_records import StrategyRun, StrategyTrade, StrategyEvent

IST = ZoneInfo("Asia/Kolkata")
KEY, VERSION, TAG = "groww_orb", "v1-intraday-protected", "GROWWORB"
FINAL = {"COMPLETE", "REJECTED", "CANCELLED"}
log = logging.getLogger(__name__)


def completed_minute_rows(kite, token, today, *, after=None, now=None):
    """Return strictly completed, chronological one-minute NIFTY candles."""
    now = now or datetime.now(IST)
    end = now.replace(second=0, microsecond=0)
    start = datetime.combine(today, clock_time(9, 15), IST)
    if after:
        start = after + timedelta(minutes=1)
    if end <= start:
        return []
    rows = kite.historical_data(token, start, end, "minute", continuous=False, oi=False)
    result = []
    for row in rows:
        stamp = row["date"].astimezone(IST) if row["date"].tzinfo else row["date"].replace(tzinfo=IST)
        if stamp < start or stamp >= end:
            continue
        result.append({"date": stamp, **{k: row[k] for k in ("open", "high", "low", "close")}})
    return sorted(result, key=lambda row: row["date"])


class Journal:
    def __init__(self, user_id, today, live):
        from trading.database import get_db
        self.user_id, self.today, self.live, self.trade_id = user_id, today, live, None
        now = datetime.now(timezone.utc)
        with get_db() as db:
            existing = db.query(StrategyTrade).filter(StrategyTrade.user_id == user_id,
                StrategyTrade.strategy_key == KEY, ((StrategyTrade.trading_date == today) |
                StrategyTrade.status.in_(("open", "entry_pending", "exit_pending", "reconcile")))).first()
            if existing: raise RuntimeError("Prior or unresolved Groww ORB trade exists; reconcile before restart")
            run = StrategyRun(user_id=user_id, strategy_key=KEY, strategy_version=VERSION,
                mode="live" if live else "paper", status="running", started_at=now, heartbeat_at=now,
                configuration={"source":"completed_1m_kite_candles", "lot_count":1,
                               "expiry":"nearest_unexpired", "entry":"next_minute_open",
                               "risk":"broker_stop_limit" if live else "paper_quote"})
            db.add(run); db.flush(); self.run_id = run.id
    def heartbeat(self):
        from trading.database import get_db
        with get_db() as db:
            run=db.get(StrategyRun,self.run_id); run.heartbeat_at=run.updated_at=datetime.now(timezone.utc)
    def event(self, action, reason, details=None):
        from trading.database import get_db
        with get_db() as db: db.add(StrategyEvent(strategy_run_id=self.run_id,strategy_trade_id=self.trade_id,
            occurred_at=datetime.now(timezone.utc),action=action,reason=reason,details=details or {}))
    def begin(self, signal, contract, quantity, spot):
        from trading.database import get_db
        with get_db() as db:
            trade=StrategyTrade(strategy_run_id=self.run_id,user_id=self.user_id,strategy_key=KEY,
                strategy_version=VERSION,mode="live" if self.live else "paper",trading_date=self.today,
                trade_number=1,status="entry_pending" if self.live else "open",side=signal["side"],
                option_symbol=contract["tradingsymbol"],option_expiry=contract["expiry"],option_strike=contract["strike"],
                lot_count=1,quantity=quantity,entry_signal_time=signal["signal_time"],entry_reason=signal["setup"],
                entry_index=spot,initial_stop=Decimal(str(signal["stop"])),current_stop=Decimal(str(signal["stop"])))
            db.add(trade);db.flush();self.trade_id=trade.id
    def filled(self, order_id, premium):
        from trading.database import get_db
        with get_db() as db:
            t=db.get(StrategyTrade,self.trade_id);t.entry_order_id=str(order_id) if order_id else None;t.entry_premium=premium;t.entry_confirmed_at=t.updated_at=datetime.now(timezone.utc);t.status="open"
    def protect(self, order_id, trigger, limit):
        from trading.database import get_db
        with get_db() as db:
            t=db.get(StrategyTrade,self.trade_id);t.protection_order_id=str(order_id);t.protection_trigger=trigger;t.protection_limit=limit
    def exit(self, order_id, premium, reason, spot):
        from trading.database import get_db
        with get_db() as db:
            t=db.get(StrategyTrade,self.trade_id);t.exit_order_id=str(order_id) if order_id else None;t.exit_premium=premium;t.exit_reason=reason;t.exit_index=spot;t.exit_confirmed_at=t.updated_at=datetime.now(timezone.utc);t.gross_option_pnl=(premium-t.entry_premium)*t.quantity;t.index_points=(Decimal(1) if t.side=="CE" else Decimal(-1))*(spot-t.entry_index);t.status="closed"
    def finish(self,status,message=None):
        from trading.database import get_db
        with get_db() as db:
            run=db.get(StrategyRun,self.run_id);run.status=status;run.error_message=message;run.ended_at=run.heartbeat_at=run.updated_at=datetime.now(timezone.utc)


def quote(kite, symbol):
    row=kite.quote([symbol])[symbol]; stamp=row.get("timestamp")
    if not isinstance(stamp,datetime): raise RuntimeError("Quote timestamp missing")
    if stamp.tzinfo is None: stamp=stamp.replace(tzinfo=IST)
    if datetime.now(IST)-stamp.astimezone(IST)>timedelta(seconds=20): raise RuntimeError("Quote is stale")
    return Decimal(str(row["last_price"]))

def confirm(kite, contract, transaction, quantity, journal, *, exit_order=False):
    order=place_protected_market_order(kite,variety=kite.VARIETY_REGULAR,exchange="NFO",tradingsymbol=contract["tradingsymbol"],transaction_type=transaction,quantity=quantity,product=kite.PRODUCT_MIS,order_type=kite.ORDER_TYPE_MARKET,validity=kite.VALIDITY_DAY,tag=TAG)
    for _ in range(20):
        state=kite.order_history(order)[-1]
        if state["status"]=="COMPLETE" and int(state.get("filled_quantity") or 0)==quantity: return str(order),Decimal(str(state["average_price"]))
        if state["status"] in FINAL: raise RuntimeError("Broker order %s %s"%(order,state["status"]))
        time.sleep(1)
    raise RuntimeError("Broker order unconfirmed; reconcile manually")

def install_stop(kite, contract, quantity, premium, limits, journal):
    from trading.strategy_risk import stop_limit_prices
    trigger,limit=stop_limit_prices(premium,limits.risk_per_trade,quantity,Decimal(str(contract["tick_size"])),limits.stop_limit_buffer_pct)
    order=kite.place_order(variety=kite.VARIETY_REGULAR,exchange="NFO",tradingsymbol=contract["tradingsymbol"],transaction_type=kite.TRANSACTION_TYPE_SELL,quantity=quantity,product=kite.PRODUCT_MIS,order_type=kite.ORDER_TYPE_SL,trigger_price=float(trigger),price=float(limit),validity=kite.VALIDITY_DAY,tag="GROWWORBSTOP")
    journal.protect(order,trigger,limit)
    for _ in range(10):
        if kite.order_history(order)[-1]["status"] in {"TRIGGER PENDING","OPEN"}: return
        time.sleep(.5)
    raise RuntimeError("Broker stop was not confirmed")

def close_position(kite, journal, position, spot, reason, *, live):
    """Close only the recorded lot; live exits first cancel the broker stop."""
    if not live:
        journal.exit(None,quote(kite,"NFO:"+position["contract"]["tradingsymbol"]),reason,spot); return
    from strategy_guardian import cancel_protection, finish_exit
    from trading.database import get_db
    with get_db() as db:
        trade=db.get(StrategyTrade,journal.trade_id);db.expunge(trade)
    completed=cancel_protection(kite,trade)
    if completed is not None:
        finish_exit(trade.id,trade.protection_order_id,completed["average_price"],"broker_stop"); return
    order,premium=confirm(kite,position["contract"],kite.TRANSACTION_TYPE_SELL,position["contract"]["lot_size"],journal,exit_order=True)
    journal.exit(order,premium,reason,spot)

def run(kite,user_id,*,live=False,poll_seconds=5):
    today=datetime.now(IST).date(); journal=Journal(user_id,today,live); engine=GrowwOrbIntraday(); last=None; position=None
    nse=next((x for x in kite.instruments("NSE") if x.get("tradingsymbol")=="NIFTY 50"),None)
    if not nse: raise RuntimeError("NIFTY 50 instrument token unavailable")
    instruments=kite.instruments("NFO")
    try:
        while datetime.now(IST).date()==today and datetime.now(IST).time()<clock_time(15,31):
            now=datetime.now(IST)
            if now.weekday()>=5 or now.time()<clock_time(9,16): time.sleep(poll_seconds);continue
            for bar in completed_minute_rows(kite,nse["instrument_token"],today,after=last,now=now):
                if last and bar["date"]!=last+timedelta(minutes=1): raise RuntimeError("Missing minute candle; live execution halted")
                last=bar["date"]; signals=engine.on_bar(bar); spot=Decimal(str(bar["close"]))
                if position and ((position["side"]=="CE" and spot<position["stop"]) or (position["side"]=="PE" and spot>position["stop"])):
                    close_position(kite,journal,position,spot,"range_stop",live=live)
                    position=None; continue
                if position and bar["date"].time()==clock_time(15,29):
                    close_position(kite,journal,position,spot,"session_end",live=live)
                    position=None; continue
                if position or not signals: continue
                signal=signals[0]; contract=current_atm_option(instruments,today,signal["side"],spot); quantity=int(contract["lot_size"])
                if live:
                    from trading.strategy_risk import check_new_entry
                    estimate=quote(kite,"NFO:"+contract["tradingsymbol"]); limits=check_new_entry(user_id,KEY,today,estimate,quantity)
                    occupied=sum(int(p.get("quantity") or 0) for p in kite.positions().get("net",[])
                        if p.get("exchange")=="NFO" and p.get("tradingsymbol")==contract["tradingsymbol"])
                    pending=any(o.get("exchange")=="NFO" and o.get("tradingsymbol")==contract["tradingsymbol"]
                        and o.get("status") not in FINAL for o in kite.orders())
                    if occupied or pending:
                        raise RuntimeError("Selected contract is already in use; refusing to mix a manual position")
                journal.begin(signal,contract,quantity,spot)
                if live:
                    order,premium=confirm(kite,contract,kite.TRANSACTION_TYPE_BUY,quantity,journal);journal.filled(order,premium)
                    try: install_stop(kite,contract,quantity,premium,limits,journal)
                    except Exception:
                        from strategy_guardian import emergency_exit
                        from trading.database import get_db
                        with get_db() as db: t=db.get(StrategyTrade,journal.trade_id);db.expunge(t)
                        emergency_exit(kite,t,"broker_protection_failed");raise
                else: journal.filled(None,quote(kite,"NFO:"+contract["tradingsymbol"]))
                position={**signal,"contract":contract}
            journal.heartbeat();time.sleep(poll_seconds)
        if position: raise RuntimeError("Session ended with an open option; reconcile manually")
        journal.finish("completed")
    except Exception as exc:
        journal.finish("failed",str(exc));raise

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("--user-id",type=int,required=True);p.add_argument("--live",action="store_true");p.add_argument("--poll-seconds",type=float,default=5);a=p.parse_args()
    from nifty_15m_env import load_nifty_environment;load_nifty_environment()
    from trading.user_token import fetch_user_token
    logging.basicConfig(level=logging.INFO);kite,user_id=fetch_user_token(log,a.user_id);kite.profile()
    if user_id!=a.user_id: raise RuntimeError("Authenticated account does not match requested user")
    run(kite,user_id,live=a.live,poll_seconds=a.poll_seconds)
if __name__=="__main__": main()
