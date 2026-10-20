"""Kite polling runner for NIFTY 15-minute signals (paper by default).

Kite session comes from the active user's encrypted exchange link in the DB.
Live orders require --live.
The process must remain running; index-triggered exits cannot be broker-native
option stop orders. Run this only while supervising the broker account.
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import date, datetime, timedelta, time as clock_time
from decimal import Decimal
from zoneinfo import ZoneInfo

from kiteconnect.exceptions import TokenException

from market.kite_orders import place_protected_market_order
from strategies.nifty_15m_breakout import Bar, Nifty15mBreakout


IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger(__name__)


def live_start_allowed(now: datetime) -> bool:
    """A late or weekend start must not invent missing intraday trade state."""
    return now.weekday() < 5 and clock_time(9, 15) <= now.time() < clock_time(9, 45)


def connect_live_with_token_retry(fetch_token, *, attempts=5, retry_seconds=300,
                                  now_fn=None, sleep_fn=None, on_retry=None):
    """Retry only missing/invalid Kite sessions before the safe-start cutoff."""
    now_fn = now_fn or (lambda: datetime.now(IST))
    sleep_fn = sleep_fn or time.sleep
    for attempt in range(1, attempts + 1):
        if not live_start_allowed(now_fn()):
            raise RuntimeError(
                "Live runner missed its safe 09:15-09:45 IST startup window; "
                "do not restart mid-session without reconciling broker and strategy state"
            )
        try:
            kite, user_id = fetch_token(log)
            kite.profile()  # The DB expiry alone does not prove Kite accepts the token.
            return kite, user_id
        except (TokenException, SystemExit) as exc:
            if isinstance(exc, SystemExit) and exc.code != 1:
                raise
            if attempt == attempts:
                raise RuntimeError(f"Kite session unavailable after {attempts} attempts") from exc
            log.warning("Kite session unavailable (attempt %s/%s); retrying in %s seconds",
                        attempt, attempts, retry_seconds)
            if on_retry:
                on_retry(attempt, attempts, retry_seconds)
            sleep_fn(retry_seconds)


def next_week_expiry(instruments, today: date):
    monday = today + timedelta(days=7 - today.weekday())
    sunday = monday + timedelta(days=6)
    expiries = sorted({item["expiry"] for item in instruments
                       if item.get("name") == "NIFTY" and item.get("instrument_type") in {"CE", "PE"}
                       and monday <= item["expiry"] <= sunday})
    if not expiries:
        raise RuntimeError("No next-week NIFTY option expiry in current Kite instrument list")
    return expiries[-1]


def atm_option(instruments, expiry, side, index_price):
    choices = [item for item in instruments if item.get("name") == "NIFTY"
               and item.get("instrument_type") == side and item.get("expiry") == expiry]
    if not choices:
        raise RuntimeError(f"No {side} contracts for {expiry}")
    return min(choices, key=lambda item: (abs(Decimal(str(item["strike"])) - index_price), item["strike"]))


def confirmed_order(kite, contract, transaction, quantity, *, on_placed=None):
    order_id = place_protected_market_order(
        kite, variety=kite.VARIETY_REGULAR, exchange="NFO",
        tradingsymbol=contract["tradingsymbol"], transaction_type=transaction,
        quantity=quantity, product=kite.PRODUCT_MIS, order_type=kite.ORDER_TYPE_MARKET,
        validity=kite.VALIDITY_DAY, tag="NIFTY15BREAKOUT",
    )
    if on_placed:
        try:
            on_placed(order_id)
        except Exception as exc:
            raise RuntimeError(f"Order {order_id} submitted but journal update failed; reconcile manually") from exc
    for _ in range(20):
        history = kite.order_history(order_id)
        status = history[-1]["status"]
        if status == "COMPLETE":
            average_price = Decimal(str(history[-1].get("average_price") or 0))
            if average_price <= 0:
                raise RuntimeError(f"Order {order_id} complete without a valid average fill price")
            if int(history[-1].get("filled_quantity") or 0) != quantity:
                raise RuntimeError(f"Order {order_id} has an unexpected filled quantity")
            return order_id, average_price
        if status in {"REJECTED", "CANCELLED"}:
            raise RuntimeError(f"Order {order_id} {status}: {history[-1].get('status_message')}")
        time.sleep(1)
    raise RuntimeError(f"Order {order_id} unconfirmed; inspect Kite orders before restarting")


def run(kite, *, user_id, live=False, poll_seconds=5, max_risk_points=30,
        runtime_monitor=None):
    from trading.strategy_records import StrategyRecorder

    recorder = StrategyRecorder(user_id, live=live, max_risk_points=max_risk_points)
    try:
        _run_session(kite, recorder=recorder, live=live,
                     poll_seconds=poll_seconds, max_risk_points=max_risk_points,
                     runtime_monitor=runtime_monitor)
    except KeyboardInterrupt:
        recorder.mark_pending_uncertain("Runner interrupted while an order was pending")
        recorder.finish("stopped")
        raise
    except Exception as exc:
        recorder.mark_uncertain(str(exc))
        recorder.finish("failed", str(exc))
        raise


def _run_session(kite, *, recorder, live=False, poll_seconds=5, max_risk_points=30,
                 runtime_monitor=None):
    instruments = kite.instruments("NFO")
    index_instruments = kite.instruments("NSE")
    nifty = next((item for item in index_instruments if item["tradingsymbol"] == "NIFTY 50"), None)
    if nifty is None:
        raise RuntimeError("NIFTY 50 index token not found in Kite instruments")
    token = nifty["instrument_token"]
    strategy = Nifty15mBreakout(max_risk_points=max_risk_points)
    last_time = None
    initialized = False
    contract = None
    position_date = None
    today = datetime.now(IST).date()
    expiry = next_week_expiry(instruments, today)
    if live:
        pending = [o for o in kite.orders()
                   if o.get("tag") == "NIFTY15BREAKOUT"
                   and o.get("status") not in {"COMPLETE", "REJECTED", "CANCELLED"}]
        if pending:
            raise RuntimeError("Unresolved strategy order found; reconcile in Kite before restarting")
        existing = [p for p in kite.positions().get("net", [])
                    if p.get("exchange") == "NFO" and str(p.get("tradingsymbol", "")).startswith("NIFTY")
                    and int(p.get("quantity", 0)) != 0]
        if existing:
            raise RuntimeError("Existing NIFTY option position found; reconcile before starting live runner")
    log.info("NIFTY token=%s; next-week expiry=%s; mode=%s", token, expiry, "LIVE" if live else "PAPER")

    while True:
        now = datetime.now(IST)
        if now.date() != today:
            if strategy.position:
                raise RuntimeError("Position remains open across sessions; reconcile manually")
            today = now.date()
            instruments = kite.instruments("NFO")
            expiry = next_week_expiry(instruments, today)
            strategy = Nifty15mBreakout(max_risk_points=max_risk_points)
            last_time = None
            initialized = False
        if now.weekday() >= 5 or now.time() < clock_time(9, 30) or now.time() > clock_time(15, 30):
            time.sleep(poll_seconds)
            continue

        records = kite.historical_data(token, f"{now.date()} 09:15:00",
                                       f"{now.date()} 15:30:00", "15minute")
        completed = []
        for record in records:
            bar_time = record["date"]
            if bar_time.tzinfo is None:
                bar_time = bar_time.replace(tzinfo=IST)
            if bar_time + timedelta(minutes=15, seconds=5) > now:
                continue
            bar = Bar(bar_time, *(Decimal(str(record[key])) for key in ("open", "high", "low", "close")))
            completed.append(bar)
        if not initialized:
            if completed:
                if completed[-1].time.time() > clock_time(9, 15):
                    raise RuntimeError(
                        "Runner started after the first 15-minute candle; "
                        "today's entry/re-entry state cannot be reconstructed safely. "
                        "Start before the 09:30 candle closes on the next session."
                    )
                strategy.previous = completed[-1]
                last_time = completed[-1].time
                initialized = True
                log.info("Primed from completed candle %s; awaiting next close", last_time)
                if runtime_monitor:
                    runtime_monitor.success("Primed from first NIFTY candle; awaiting breakout")
            time.sleep(poll_seconds)
            continue
        for bar in completed:
            bar_time = bar.time
            if last_time and bar_time <= last_time:
                continue
            if now - (bar_time + timedelta(minutes=15)) > timedelta(minutes=2):
                raise RuntimeError("Missed candle close by over 2 minutes; restart and reconcile")
            decision = strategy.on_bar(bar)
            last_time = bar_time
            if decision.action == "HOLD":
                if decision.reason in {"risk_cap", "trade_limit"}:
                    log.info("Skipped breakout: %s", decision.reason)
                    recorder.event(decision)
                elif decision.reason == "trail_stop":
                    log.info("Trailed %s NIFTY stop to %s after close=%s",
                             strategy.position, decision.stop, decision.index_price)
                    recorder.trail_stop(decision)
                else:
                    recorder.heartbeat()
                continue
            log.info("%s NIFTY=%s stop=%s target=%s reason=%s", decision.action,
                     decision.index_price, decision.stop, decision.target, decision.reason)
            if decision.action.startswith("BUY"):
                contract = atm_option(instruments, expiry, decision.action[-2:], decision.index_price)
                quantity = int(contract["lot_size"])  # one exchange lot
                if quantity < 1:
                    raise RuntimeError("Selected option has an invalid lot size")
                log.info("Selected %s quantity=%s", contract["tradingsymbol"], quantity)
                recorder.begin_trade(decision, contract, quantity, strategy.entry_count)
                if live:
                    order_id, premium = confirmed_order(
                        kite, contract, kite.TRANSACTION_TYPE_BUY, quantity,
                        on_placed=lambda order_id: recorder.order_submitted(order_id, exit_order=False))
                    recorder.entry_filled(order_id, premium)
                if runtime_monitor:
                    runtime_monitor.success(f"{decision.action} {contract['tradingsymbol']} filled" if live
                                            else f"{decision.action} paper signal recorded")
                position_date = today
            else:
                if contract is None or position_date != today:
                    raise RuntimeError("No matching option position; reconcile manually")
                recorder.exit_intent(decision)
                if live:
                    order_id, premium = confirmed_order(
                        kite, contract, kite.TRANSACTION_TYPE_SELL, int(contract["lot_size"]),
                        on_placed=lambda order_id: recorder.order_submitted(order_id, exit_order=True))
                    recorder.exit_filled(order_id, premium)
                else:
                    recorder.paper_exit_complete()
                if runtime_monitor:
                    runtime_monitor.success(f"{decision.action} completed: {decision.reason}")
                contract = None
                position_date = None
        time.sleep(poll_seconds)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Place real one-lot NFO market orders")
    parser.add_argument("--poll-seconds", type=int, default=5)
    parser.add_argument("--token-attempts", type=int, default=1,
                        help="Live-mode Kite session attempts before giving up (default: 1)")
    parser.add_argument("--token-retry-seconds", type=int, default=300,
                        help="Seconds between live-mode token attempts (default: 300)")
    risk_group = parser.add_mutually_exclusive_group()
    risk_group.add_argument("--max-risk-points", type=Decimal, default=Decimal(30),
                            help="Maximum distance from NIFTY entry signal to stop (default: 30)")
    risk_group.add_argument("--no-risk-cap", dest="max_risk_points", action="store_const", const=None,
                            help="Use the uncapped baseline strategy")
    args = parser.parse_args()
    if args.poll_seconds < 1:
        parser.error("--poll-seconds must be positive")
    if args.token_attempts < 1 or args.token_retry_seconds < 1:
        parser.error("--token-attempts and --token-retry-seconds must be positive")
    if args.max_risk_points is not None and args.max_risk_points <= 0:
        parser.error("--max-risk-points must be positive")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from nifty_15m_env import load_nifty_environment
    load_nifty_environment()

    from trading.user_token import fetch_user_token
    from trading.service_runtime import ServiceRuntimeMonitor
    runtime_monitor = ServiceRuntimeMonitor(
        "nifty_15m_v3", log, token_alerts_during_market_session=True)
    runtime_monitor.start("Checking Kite session")
    try:
        if args.live:
            from market.trading_calendar import is_nse_trading_day
            if not is_nse_trading_day(datetime.now(IST).date()):
                runtime_monitor.stop("NSE holiday or weekend; no strategy run")
                return
        if args.live:
            kite, user_id = connect_live_with_token_retry(
                fetch_user_token, attempts=args.token_attempts,
                retry_seconds=args.token_retry_seconds,
                on_retry=lambda attempt, total, seconds: runtime_monitor.degraded(
                    f"Kite session unavailable; retry {attempt + 1}/{total} in {seconds // 60} minutes"))
        else:
            kite, user_id = fetch_user_token(log)
        runtime_monitor.user_id = user_id
        runtime_monitor.success("Kite authenticated; monitoring NIFTY 15-minute closes")
        run(kite, user_id=user_id, live=args.live, poll_seconds=args.poll_seconds,
            max_risk_points=args.max_risk_points, runtime_monitor=runtime_monitor)
        runtime_monitor.stop("Trading session complete")
    except KeyboardInterrupt:
        runtime_monitor.stop("Strategy stopped")
        raise
    except Exception as exc:
        runtime_monitor.exception(exc, "NIFTY 15-minute strategy")
        raise


if __name__ == "__main__":
    main()
