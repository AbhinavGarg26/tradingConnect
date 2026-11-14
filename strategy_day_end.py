"""Backfill missing paper strategy trades for the latest trading sessions.

The job is deliberately broker read-only.  It replays NIFTY index candles and
only inserts a paper/index-proxy trade when no live or paper trade with the same
strategy, side and entry signal already exists.  Run it after the market closes.
"""
from __future__ import annotations

import argparse
import logging
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from strategies.nifty_opening_range import IST

log = logging.getLogger(__name__)

VERSIONS = {
    "nifty_15m_breakout": "v3",
    "nifty_30m_reference": "pe1-v1",
    "fixed_profit_filtered": "v1-fixed-entries",
    "nifty_daily_ema_monthly": "ema20_50_calls_monthly_v2",
    "nifty_bearish_15m_9_21": "bearish_15m_9_21_v1",
    "nifty_bullish_15m_9_21": "bullish_15m_9_21_v1",
    "one_direction_profit_lock": "v2.1-paper",
}
RESEARCH_REPLAY_KEYS = {"one_direction_profit_lock"}
PROXY_NOTE = "Day-end paper index proxy; option premium was not reconstructed."


def is_active(details):
    """A scheduled timer or a running service makes a strategy active."""
    return (details or {}).get("service_state") in {"active", "activating", "reloading"} or (
        (details or {}).get("timer_state") == "active"
        and (details or {}).get("timer_enabled") == "enabled"
    )


def recent_session_dates(rows, count=10):
    dates = sorted({_stamp(row["date"]).date() for row in rows})
    return dates[-count:]


def _stamp(value):
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime.combine(value, time(9, 15), tzinfo=IST)
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return stamp.replace(tzinfo=IST) if stamp.tzinfo is None else stamp.astimezone(IST)


def _fetch(kite, token, start, end, interval):
    rows = []
    cursor = start
    chunk = 29 if interval != "minute" else 20
    while cursor <= end:
        finish = min(cursor + timedelta(days=chunk), end)
        values = kite.historical_data(token, f"{cursor} 09:15:00", f"{finish} 15:30:00", interval)
        duration = {"minute": 1, "15minute": 15, "day": 375}[interval]
        rows.extend({"date": item["date"], "duration_minutes": duration,
                     **{key: item[key] for key in ("open", "high", "low", "close")}}
                    for item in values)
        cursor = finish + timedelta(days=1)
    return rows


def _as_trade(item, key):
    """Normalize the existing backtest result shapes into journal fields."""
    if hasattr(item, "entry_time"):
        return dict(side=item.side, entry_time=_stamp(item.entry_time), exit_time=_stamp(item.exit_time),
                    entry=Decimal(str(item.entry_price)), exit=Decimal(str(item.exit_price)),
                    points=Decimal(str(item.points)), stop=Decimal(str(item.stop)),
                    target=None, reason=item.exit_reason)
    side = item["side"]
    entry_time = _stamp(item.get("entry_time") or item.get("date"))
    exit_time = _stamp(item.get("exit_time") or item.get("exit_bar_time") or entry_time)
    entry = Decimal(str(item.get("entry", item.get("entry_price"))))
    points = Decimal(str(item["points"]))
    exit_value = item.get("exit", item.get("exit_price"))
    exit_price = (Decimal(str(exit_value)) if exit_value is not None else
                  entry + (points if side == "CE" else -points))
    return dict(side=side, entry_time=entry_time, exit_time=exit_time,
                entry=entry, exit=exit_price, points=points,
                stop=Decimal(str(item["stop"])) if item.get("stop") is not None else None,
                target=Decimal(str(item["target"])) if item.get("target") is not None else None,
                reason=item.get("exit_reason", "ema_reversal"))


def _complete_minute_sessions(rows):
    """Return regular 09:15-15:29 sessions and ignore partial broker data."""
    grouped = {}
    for row in rows:
        stamp = _stamp(row["date"])
        grouped.setdefault(stamp.date(), []).append(row)
    complete = []
    for _day, session in sorted(grouped.items()):
        session.sort(key=lambda row: _stamp(row["date"]))
        stamps = [_stamp(row["date"]) for row in session]
        if (len(session) == 375 and stamps[0].time() == time(9, 15)
                and stamps[-1].time() == time(15, 29)
                and all(right - left == timedelta(minutes=1)
                        for left, right in zip(stamps, stamps[1:]))):
            complete.append(session)
    return complete


def _one_direction_trades(fifteen_rows):
    """Replay direction/reversal exits only; premium stops are unavailable."""
    import pandas as pd
    from indicators.adx import compute_adx
    from strategies.one_direction_profit_lock import OneDirectionProfitLock, TrendSnapshot

    if not fifteen_rows:
        return []
    frame = pd.DataFrame([{
        "time": _stamp(row["date"]),
        **{name: float(row[name]) for name in ("open", "high", "low", "close")},
    } for row in fifteen_rows]).sort_values("time").reset_index(drop=True)
    frame["ema_fast"] = frame.close.ewm(span=9, adjust=False).mean()
    frame["ema_slow"] = frame.close.ewm(span=21, adjust=False).mean()
    frame = compute_adx(frame, period=14)
    frame["session"] = frame.time.map(lambda value: value.date())
    frame["typical"] = (frame.high + frame.low + frame.close) / 3
    frame["reference"] = frame.groupby("session").typical.expanding().mean().reset_index(level=0, drop=True)

    trades = []
    for _day, session in frame.groupby("session", sort=True):
        gate, active = OneDirectionProfitLock(), None
        rows = list(session.itertuples(index=False))
        for index, row in enumerate(rows):
            if pd.isna(row.adx) or pd.isna(row.plus_di) or pd.isna(row.minus_di):
                continue
            observed = row.time + timedelta(minutes=15)
            snapshot = TrendSnapshot(observed, *(Decimal(str(value)) for value in (
                row.close, row.ema_fast, row.ema_slow, row.reference,
                row.adx, row.plus_di, row.minus_di,
            )))
            reversal = gate.observe_reversal(snapshot) if active else None
            session_end = index == len(rows) - 1 or observed.astimezone(IST).time() >= time(15, 15)
            if active and (reversal or session_end):
                direction = Decimal("1") if active["side"] == "CE" else Decimal("-1")
                exit_price = Decimal(str(row.close))
                trades.append({**active, "exit_time": observed.isoformat(),
                               "exit": float(exit_price),
                               "exit_reason": "reversal" if reversal else "session_end",
                               "points": float(direction * (exit_price - Decimal(str(active["entry"]))))})
                if reversal:
                    gate.accept_reversal(reversal, observed)
                gate.record_exit(observed)
                active = None
            if active is None and not session_end:
                side = gate.market_direction(snapshot)
                if side and gate.allow_entry(side, snapshot) == (True, "allowed"):
                    gate.record_entry(side, snapshot)
                    active = {"date": str(observed.date()), "side": side,
                              "entry_time": observed.isoformat(), "entry": float(row.close),
                              "stop": None, "target": None}
    return trades


def replay(key, minute_rows, fifteen_rows, daily_rows, keep_dates):
    """Replay the production decision engines and return closed index trades."""
    raw = []
    if key == "nifty_15m_breakout":
        from backtest.nifty_15m_breakout import completed_trades, run
        raw, _ = completed_trades(run(fifteen_rows))
    elif key == "nifty_30m_reference":
        from backtest.nifty_30m_reference import run
        for session in _complete_minute_sessions(minute_rows):
            trades, *_ = run(session, stop_points=25, max_entries=1,
                              buffer_percent=5, first_signal_pe=True)
            raw.extend(trades)
    elif key == "fixed_profit_filtered":
        from backtest.fixed_profit_filtered import replay as replay_session
        # Reuse the loader's strict complete-session checks without a temporary file.
        from strategies.nifty_15m_breakout import Bar
        raw = [trade for session in _complete_minute_sessions(minute_rows)
               if (trade := replay_session([
                   Bar(_stamp(row["date"]), *(Decimal(str(row[k]))
                       for k in ("open", "high", "low", "close")))
                   for row in session]))]
    elif key == "nifty_bullish_15m_9_21":
        from backtest.nifty_bullish_ema_timeframes import run
        _report, raw, _signals, _curve = run(
            fifteen_rows, 9, 21, timeframe="15m",
            evaluation_start=str(min(keep_dates)), slippage_points=0)
    elif key == "nifty_bearish_15m_9_21":
        from backtest.nifty_30m_put_comparison import run
        _report, raw, _signals, _curve = run(
            fifteen_rows, 9, 21, timeframe_minutes=15,
            evaluation_start=str(min(keep_dates)), slippage_points=0)
    elif key == "nifty_daily_ema_monthly":
        from backtest.nifty_daily_ema import Config, run
        _report, raw, _curve, _signals = run(
            daily_rows, Config(fast=20, slow=50), evaluation_start=str(min(keep_dates)))
        raw = [dict(item, side="CE", entry_time=item["entry_date"],
                    exit_time=item["exit_date"], exit_reason="ema_reversal") for item in raw]
    elif key == "one_direction_profit_lock":
        raw = _one_direction_trades(fifteen_rows)
    trades = [_as_trade(item, key) for item in raw]
    return [trade for trade in trades if trade["entry_time"].date() in keep_dates]


def persist(factory, user_id, key, trades, now):
    """Insert missing signals atomically; any existing mode takes precedence."""
    from sqlalchemy import select
    from trading.strategy_records import StrategyRun, StrategyTrade
    inserted = 0
    with factory() as db:
        missing = []
        for trade in trades:
            exists = db.scalar(select(StrategyTrade.id).where(
                StrategyTrade.user_id == user_id, StrategyTrade.strategy_key == key,
                StrategyTrade.side == trade["side"],
                StrategyTrade.entry_signal_time == trade["entry_time"]).limit(1))
            if not exists:
                missing.append(trade)
        if not missing:
            return 0
        run = StrategyRun(user_id=user_id, strategy_key=key, strategy_version=VERSIONS[key],
                          mode="paper", status="completed", started_at=now, ended_at=now,
                          heartbeat_at=now, configuration={"day_end_backfill": True, "lookback_sessions": 10,
                                                          "price_source": "nifty_index"})
        db.add(run); db.flush()
        per_day = {}
        for trade in missing:
            day = trade["entry_time"].date()
            per_day[day] = per_day.get(day, 0) + 1
            strike = (trade["entry"] / Decimal("50")).quantize(Decimal("1")) * Decimal("50")
            db.add(StrategyTrade(
                strategy_run_id=run.id, user_id=user_id, strategy_key=key,
                strategy_version=VERSIONS[key], mode="paper", trading_date=day,
                trade_number=per_day[day], status="closed", side=trade["side"],
                option_symbol=f"NIFTY INDEX PROXY {trade['side']}", option_expiry=day,
                option_strike=strike, lot_count=1, quantity=1,
                entry_signal_time=trade["entry_time"], exit_signal_time=trade["exit_time"],
                entry_confirmed_at=trade["entry_time"], exit_confirmed_at=trade["exit_time"],
                entry_reason="day_end_replay", exit_reason=trade["reason"],
                entry_index=trade["entry"], exit_index=trade["exit"],
                initial_stop=trade["stop"], current_stop=trade["stop"], target=trade["target"],
                index_points=trade["points"], error_message=PROXY_NOTE))
            inserted += 1
    return inserted


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--sessions", type=int, default=10, choices=(10,), help="Fixed safety window")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from nifty_15m_env import load_nifty_environment
    load_nifty_environment()
    from trading.database import get_db
    from trading.user_token import fetch_user_token
    from sqlalchemy import text
    with get_db() as db:
        observations = db.execute(text("SELECT strategy_key, details FROM strategy_service_observations WHERE user_id=:uid"), {"uid": args.user_id}).all()
    active = sorted({key for key, details in observations if key in VERSIONS and is_active(details)} |
                    RESEARCH_REPLAY_KEYS)
    if not active:
        log.info("No active strategies; nothing to backfill")
        return
    kite, user_id = fetch_user_token(log, user_id=args.user_id)
    nifty = next(item for item in kite.instruments("NSE") if item["tradingsymbol"] == "NIFTY 50")
    today = datetime.now(IST).date()
    start = today - timedelta(days=120)
    daily = _fetch(kite, nifty["instrument_token"], start, today, "day")
    sessions = set(recent_session_dates(daily, args.sessions))
    intraday_start = min(sessions) - timedelta(days=35)  # EMA warm-up.
    fifteen = _fetch(kite, nifty["instrument_token"], intraday_start, today, "15minute")
    minute = _fetch(kite, nifty["instrument_token"], min(sessions), today, "minute")
    total = 0
    for key in active:
        trades = replay(key, minute, fifteen, daily, sessions)
        count = 0 if args.dry_run else persist(get_db, user_id, key, trades, datetime.now(timezone.utc))
        total += count
        log.info("%s: replayed=%d inserted=%d", key, len(trades), count)
    log.info("Day-end backfill complete: inserted=%d active_strategies=%d", total, len(active))


if __name__ == "__main__":
    main()
