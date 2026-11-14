"""Paper-forward service for One Direction Profit Lock V2.1.

The service deliberately has no live-order flag.  It uses completed NIFTY
15-minute candles, selects the nearest unexpired weekly ATM option, observes
real option quotes, and records simulated fills in the service log.  Live
promotion requires a separate review of paper stop-fill evidence.
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, time as clock_time, timezone
from decimal import Decimal

import pandas as pd
from sqlalchemy import text

from indicators.adx import compute_adx
from strategies.nifty_opening_range import IST, current_atm_option
from strategies.one_direction_profit_lock import OneDirectionProfitLock, TrendSnapshot

log = logging.getLogger("one_direction_profit_lock")
SERVICE_NAME = "one-direction-profit-lock"


def nearest_weekly_atm(instruments, today, side, spot):
    """Nearest listed unexpired NIFTY expiry and nearest strike."""
    return current_atm_option(instruments, today, side, spot)


def completed_trend_snapshot(now: datetime) -> TrendSnapshot | None:
    """Build a trend snapshot from database 15-minute candles only."""
    from trading.database import get_db
    with get_db() as db:
        rows = list(db.execute(text("""
            SELECT captured_at, open_price, high_price, low_price, close_price
              FROM market_snapshots
             WHERE symbol = 'NIFTY 50' AND timeframe = '15m'
               AND captured_at < :cutoff
             ORDER BY captured_at DESC LIMIT 120
        """), {"cutoff": now.astimezone(timezone.utc).replace(tzinfo=None)}))
    if len(rows) < 30:
        return None
    rows.reverse()
    frame = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close"])
    for column in ("open", "high", "low", "close"):
        frame[column] = frame[column].astype(float)
    frame["ema_fast"] = frame.close.ewm(span=9, adjust=False).mean()
    frame["ema_slow"] = frame.close.ewm(span=21, adjust=False).mean()
    frame = compute_adx(frame, period=14)
    local_dates = [
        (stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp)
        .astimezone(IST).date() for stamp in frame.time
    ]
    frame["session"] = local_dates
    frame["typical"] = (frame.high + frame.low + frame.close) / 3
    # NIFTY cash index has no exchange volume. This is an explicit session
    # typical-price anchor, not a volume-weighted claim.
    frame["reference"] = frame.groupby("session").typical.expanding().mean().reset_index(level=0, drop=True)
    row = frame.iloc[-1]
    if pd.isna(row[["adx", "plus_di", "minus_di"]]).any():
        return None
    observed = rows[-1][0]
    observed = observed.replace(tzinfo=timezone.utc) if observed.tzinfo is None else observed
    return TrendSnapshot(
        observed.astimezone(IST), *(Decimal(str(row[name])) for name in (
            "close", "ema_fast", "ema_slow", "reference", "adx", "plus_di", "minus_di"
        )))


def option_quote(kite, contract) -> Decimal:
    key = f"NFO:{contract['tradingsymbol']}"
    price = Decimal(str(kite.ltp([key])[key]["last_price"]))
    if not price.is_finite() or price <= 0:
        raise RuntimeError("Invalid option quote")
    return price


def run(kite, user_id: int, *, poll_seconds=5, check=False):
    instruments = kite.instruments("NFO")
    if check:
        spot = Decimal(str(kite.ltp(["NSE:NIFTY 50"])["NSE:NIFTY 50"]["last_price"]))
        contracts = {side: nearest_weekly_atm(instruments, datetime.now(IST).date(), side, spot)
                     for side in ("CE", "PE")}
        for side, contract in contracts.items():
            print(f"{side}: {contract['tradingsymbol']} expiry={contract['expiry']} strike={contract['strike']}")
        print("orders: DISABLED (paper service)")
        return

    gate = OneDirectionProfitLock()
    position = None
    last_candle = None
    session_date = datetime.now(IST).date()
    while True:
        now = datetime.now(IST)
        if now.date() != session_date:
            if position:
                raise RuntimeError("Paper position crossed session boundary; reconcile the prior run")
            session_date = now.date()
            gate.reset()
            last_candle = None
            instruments = kite.instruments("NFO")
        if now.weekday() >= 5 or not clock_time(9, 15) <= now.time() <= clock_time(15, 20):
            time.sleep(poll_seconds); continue
        snapshot = completed_trend_snapshot(now)
        if snapshot is None:
            time.sleep(poll_seconds); continue
        if position:
            ltp = option_quote(kite, position["contract"])
            position["peak"] = max(position["peak"], ltp)
            trailing = gate.protected_stop(position["entry"], position["peak"], position["stop"])
            if trailing is not None:
                position["stop"] = trailing
            reversal = gate.observe_reversal(snapshot)
            reason = ("session_end" if now.time() >= clock_time(15, 15)
                      else "stop" if ltp <= position["stop"]
                      else "reversal" if reversal else None)
            if reason:
                pnl = (ltp - position["entry"]) * position["quantity"]
                log.info("PAPER EXIT %s price=%s pnl=%s reason=%s",
                         position["contract"]["tradingsymbol"], ltp, pnl, reason)
                gate.record_closed_trade(pnl)
                if reversal and not gate.lock_reason:
                    gate.accept_reversal(reversal, snapshot.observed_at)
                gate.record_exit(now)
                position = None
            time.sleep(poll_seconds); continue
        if last_candle == snapshot.observed_at:
            time.sleep(poll_seconds); continue
        last_candle = snapshot.observed_at
        side = gate.market_direction(snapshot)
        if side is None:
            continue
        allowed, reason = gate.allow_entry(side, snapshot)
        if not allowed:
            log.info("PAPER ENTRY BLOCKED side=%s reason=%s", side, reason); continue
        contract = nearest_weekly_atm(instruments, now.date(), side, snapshot.close)
        premium = option_quote(kite, contract)
        stop = gate.initial_stop(premium, 10)
        quantity = gate.risk_sized_quantity(premium, stop, int(contract["lot_size"]))
        gate.record_entry(side, snapshot)
        position = {"contract": contract, "entry": premium, "peak": premium,
                    "stop": stop, "quantity": quantity}
        log.info("PAPER BUY %s qty=%s premium=%s stop=%s expiry=%s",
                 contract["tradingsymbol"], quantity, premium, stop, contract["expiry"])


def main():
    from trading.service_runtime import ServiceRuntimeMonitor
    from trading.user_token import fetch_user_token
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=5)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    monitor = ServiceRuntimeMonitor(SERVICE_NAME, log, token_alerts_during_market_session=True)
    monitor.start("Starting paper-forward strategy")
    try:
        kite, user_id = fetch_user_token(log, args.user_id)
        run(kite, user_id, poll_seconds=args.poll_seconds, check=args.check)
        monitor.success("Paper-forward session completed")
    except Exception as exc:
        monitor.exception(exc, "Paper-forward strategy failed")
        raise
    finally:
        monitor.stop()


if __name__ == "__main__":
    main()
