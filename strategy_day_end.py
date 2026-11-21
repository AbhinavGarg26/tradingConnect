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
    "smallcap_momentum_swing": "v2-first-15m-strength-confirmation",
    "rsi_highway": "v2-rising-weekly-confirmation",
    "gap_up_volume_confirmed": "v1-prior-volume-15m-confirmed",
}
EQUITY_REPLAY_KEYS = {"smallcap_momentum_swing", "rsi_highway", "gap_up_volume_confirmed"}
RESEARCH_REPLAY_KEYS = {"one_direction_profit_lock", *EQUITY_REPLAY_KEYS}
PROXY_NOTE = "Day-end paper index proxy; option premium was not reconstructed."
OPTION_REPLAY_NOTE = "Day-end paper option replay from Kite minute candles; OHLC fill model with adverse slippage."
OPTION_SLIPPAGE_TICKS = Decimal("2")


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


def _tick_price(value, tick_size):
    """Round down to an exchange-valid price without making a fill better."""
    tick = Decimal(str(tick_size))
    return (Decimal(str(value)) / tick).to_integral_value(rounding="ROUND_DOWN") * tick


def _option_candles(kite, contract, start, finish):
    """Read only the minute candles needed to value one historical option trade."""
    values = kite.historical_data(
        contract["instrument_token"],
        start.strftime("%Y-%m-%d %H:%M:%S"),
        finish.strftime("%Y-%m-%d %H:%M:%S"),
        "minute",
    )
    return [{"time": _stamp(row["date"]),
             **{name: Decimal(str(row[name])) for name in ("open", "high", "low", "close")}}
            for row in values]


def _first_candle(candles, at_or_after):
    return next((candle for candle in candles if candle["time"] >= at_or_after), None)


def _replay_profit_lock_option_trade(kite, instruments, signal, *, slippage_ticks=OPTION_SLIPPAGE_TICKS):
    """Value a Profit Lock signal using the selected option's actual minute candles.

    Kite historical candles have no bid/ask depth.  The fill model is therefore
    intentionally adverse: buy at candle open + slippage and sell at candle
    open/stop - slippage.  A missing contract or candle returns ``None`` rather
    than fabricating an index-proxy result.
    """
    from strategies.nifty_opening_range import current_atm_option
    from strategies.one_direction_profit_lock import OneDirectionProfitLock

    contract = current_atm_option(instruments, signal["entry_time"].date(), signal["side"], signal["entry"])
    tick = Decimal(str(contract.get("tick_size") or "0.05"))
    slip = Decimal(str(slippage_ticks)) * tick
    candles = _option_candles(kite, contract, signal["entry_time"], signal["exit_time"])
    entry_candle = _first_candle(candles, signal["entry_time"])
    exit_candle = _first_candle(candles, signal["exit_time"])
    if entry_candle is None or exit_candle is None:
        return None

    entry = _tick_price(entry_candle["open"] + slip, tick)
    if entry <= 0:
        return None
    gate = OneDirectionProfitLock()
    initial_stop = _tick_price(gate.initial_stop(entry), tick)
    stop, peak = initial_stop, entry
    exit_time, exit_reason, exit_price = signal["exit_time"], signal["reason"], None

    # Candle ordering is deliberately conservative and avoids hindsight: first
    # test the stop available at the candle open, then raise the trailing stop
    # from that candle's high for the following minute.
    for candle in candles:
        if candle["time"] <= entry_candle["time"] or candle["time"] > signal["exit_time"]:
            continue
        if candle["low"] <= stop:
            raw_fill = min(candle["open"], stop)
            exit_price = _tick_price(max(tick, raw_fill - slip), tick)
            exit_time, exit_reason = candle["time"], "stop"
            break
        peak = max(peak, candle["high"])
        trailed = gate.protected_stop(entry, peak, stop)
        if trailed is not None:
            stop = _tick_price(trailed, tick)

    if exit_price is None:
        exit_price = _tick_price(max(tick, exit_candle["open"] - slip), tick)

    quantity = int(contract.get("lot_size") or 0)
    if quantity < 1 or exit_price <= 0:
        return None
    return {
        **signal,
        "exit_time": exit_time,
        "reason": exit_reason,
        "option_symbol": contract["tradingsymbol"],
        "option_expiry": contract["expiry"],
        "option_strike": Decimal(str(contract["strike"])),
        "quantity": quantity,
        "entry_premium": entry,
        "exit_premium": exit_price,
        "gross_option_pnl": (exit_price - entry) * quantity,
        "initial_stop": initial_stop,
        "current_stop": stop,
        "option_replay": True,
    }


def _one_direction_option_trades(kite, instruments, fifteen_rows, minute_rows):
    """Return only auditable option-priced replays; never fall back to proxies."""
    output = []
    for signal in _one_direction_trades(fifteen_rows):
        signal = _as_trade(signal, "one_direction_profit_lock")
        try:
            valued = _replay_profit_lock_option_trade(kite, instruments, signal)
        except Exception as exc:
            log.warning("Skipping Profit Lock option replay %s %s: %s", signal["entry_time"], signal["side"], exc)
            continue
        if valued is not None:
            # Keep the underlying diagnostic aligned with the actual option
            # exit minute, rather than falsely reporting a full-day hold.
            entry_index = next((Decimal(str(row["close"])) for row in minute_rows
                                if _stamp(row["date"]) >= valued["entry_time"]), None)
            exit_index = next((Decimal(str(row["close"])) for row in minute_rows
                               if _stamp(row["date"]) >= valued["exit_time"]), None)
            if entry_index is not None and exit_index is not None:
                valued["entry"] = entry_index
                valued["exit"] = exit_index
                valued["points"] = (Decimal("1") if valued["side"] == "CE" else Decimal("-1")) * (exit_index - entry_index)
            output.append(valued)
    return output


def replay(key, minute_rows, fifteen_rows, daily_rows, keep_dates, *, kite=None, nfo_instruments=None):
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
        if kite is None or nfo_instruments is None:
            raise ValueError("Profit Lock option replay requires Kite and NFO instruments")
        raw = _one_direction_option_trades(kite, nfo_instruments, fifteen_rows, minute_rows)
    trades = [item if isinstance(item, dict) and item.get("option_replay") else _as_trade(item, key)
              for item in raw]
    return [trade for trade in trades if trade["entry_time"].date() in keep_dates]


def persist(factory, user_id, key, trades, now):
    """Insert missing signals atomically; any existing mode takes precedence."""
    from sqlalchemy import select
    from trading.strategy_records import StrategyRun, StrategyTrade
    inserted = 0
    with factory() as db:
        missing = []
        upgraded = 0
        for trade in trades:
            existing = db.scalar(select(StrategyTrade).where(
                StrategyTrade.user_id == user_id, StrategyTrade.strategy_key == key,
                StrategyTrade.side == trade["side"],
                StrategyTrade.entry_signal_time == trade["entry_time"]).limit(1))
            if existing is None:
                missing.append(trade)
            elif (key == "one_direction_profit_lock" and trade.get("option_replay")
                  and existing.mode == "paper"
                  and (existing.error_message or "").startswith(PROXY_NOTE)):
                # Replace a previously inserted index proxy only when the exact
                # same signal now has auditable option candles.  Live records
                # and normal paper-forward records are never changed here.
                existing.option_symbol = trade["option_symbol"]
                existing.option_expiry = trade["option_expiry"]
                existing.option_strike = trade["option_strike"]
                existing.quantity = trade["quantity"]
                existing.entry_index = trade["entry"]
                existing.exit_index = trade["exit"]
                existing.index_points = trade["points"]
                existing.exit_signal_time = trade["exit_time"]
                existing.exit_confirmed_at = trade["exit_time"]
                existing.exit_reason = trade["reason"]
                existing.initial_stop = trade["initial_stop"]
                existing.current_stop = trade["current_stop"]
                existing.entry_premium = trade["entry_premium"]
                existing.exit_premium = trade["exit_premium"]
                existing.gross_option_pnl = trade["gross_option_pnl"]
                existing.error_message = OPTION_REPLAY_NOTE
                existing.updated_at = now
                upgraded += 1
        if not missing:
            return upgraded
        run = StrategyRun(user_id=user_id, strategy_key=key, strategy_version=VERSIONS[key],
                          mode="paper", status="completed", started_at=now, ended_at=now,
                          heartbeat_at=now, configuration={"day_end_backfill": True, "lookback_sessions": 10,
                                                          "price_source": "option_ohlc" if key == "one_direction_profit_lock" else "nifty_index",
                                                          "option_slippage_ticks": str(OPTION_SLIPPAGE_TICKS) if key == "one_direction_profit_lock" else None})
        db.add(run); db.flush()
        per_day = {}
        for trade in missing:
            day = trade["entry_time"].date()
            per_day[day] = per_day.get(day, 0) + 1
            option_replay = trade.get("option_replay", False)
            strike = (trade["option_strike"] if option_replay else
                      (trade["entry"] / Decimal("50")).quantize(Decimal("1")) * Decimal("50"))
            db.add(StrategyTrade(
                strategy_run_id=run.id, user_id=user_id, strategy_key=key,
                strategy_version=VERSIONS[key], mode="paper", trading_date=day,
                trade_number=per_day[day], status="closed", side=trade["side"],
                option_symbol=trade["option_symbol"] if option_replay else f"NIFTY INDEX PROXY {trade['side']}",
                option_expiry=trade["option_expiry"] if option_replay else day,
                option_strike=strike, lot_count=1, quantity=trade["quantity"] if option_replay else 1,
                entry_signal_time=trade["entry_time"], exit_signal_time=trade["exit_time"],
                entry_confirmed_at=trade["entry_time"], exit_confirmed_at=trade["exit_time"],
                entry_reason="day_end_replay", exit_reason=trade["reason"],
                entry_index=trade["entry"], exit_index=trade["exit"],
                initial_stop=trade["initial_stop"] if option_replay else trade["stop"],
                current_stop=trade["current_stop"] if option_replay else trade["stop"], target=trade["target"],
                entry_premium=trade.get("entry_premium"), exit_premium=trade.get("exit_premium"),
                gross_option_pnl=trade.get("gross_option_pnl"), index_points=trade["points"],
                error_message=OPTION_REPLAY_NOTE if option_replay else PROXY_NOTE))
            inserted += 1
    return inserted + upgraded


def _equity_trade(symbol, entry_day, exit_day, entry, exit_price, reason, *, stop=None, entry_reason):
    entry_time = datetime.combine(entry_day, time(9, 30), tzinfo=IST)
    exit_time = datetime.combine(exit_day, time(15, 30), tzinfo=IST)
    return dict(symbol=symbol, side="EQ", entry_time=entry_time, exit_time=exit_time,
                entry=Decimal(str(entry)), exit=Decimal(str(exit_price)),
                points=Decimal(str(exit_price)) - Decimal(str(entry)),
                stop=Decimal(str(stop)) if stop is not None else None, target=None,
                reason=reason, entry_reason=entry_reason)


def replay_equity(key, keep_dates, *, now=None):
    """Replay broad-equity strategies entirely from cached database candles."""
    from backtest.equity_universe import load_equity_universe
    end = max(keep_dates)
    symbols = load_equity_universe(("nifty500", "midcap150", "smallcap500"))
    if key == "gap_up_volume_confirmed":
        from collections import defaultdict
        from dataclasses import replace
        from backtest.gap_up_volume_buildup_research import prior_volume_signal
        from backtest.nifty500_gap_up import daily_candidates, read_15m, replay
        from backtest.smallcap_momentum_swing import load_database_data
        from strategies.gap_up import StockGapConfig
        from trading.database import get_db

        data = load_database_data(symbols, end - timedelta(days=120), end,
                                  database_only=True, skip_missing_instruments=True, now=now)
        config = replace(StockGapConfig(), min_opening_relative_volume=.75,
                         min_opening_close_location=.70, max_upper_wick_ratio=.35,
                         min_gap_retained=.40)
        candidates = [row for row in daily_candidates(data, set(), price_only=True, config=config)
                      if row["date"] in keep_dates]
        indexes = {symbol: {day: index for index, (day, _) in enumerate(rows)}
                   for symbol, rows in data.items()}
        output = []
        with get_db() as db:
            for candidate in candidates:
                symbol, entry_day = candidate["symbol"], candidate["date"]
                rows = data.get(symbol, [])
                index = indexes.get(symbol, {}).get(entry_day)
                if index is None or not prior_volume_signal(rows, index, 4, 1.25, "any"):
                    continue
                start = datetime.combine(entry_day - timedelta(days=45), time.min, IST).astimezone(timezone.utc).replace(tzinfo=None)
                finish = datetime.combine(entry_day + timedelta(days=1), time.min, IST).astimezone(timezone.utc).replace(tzinfo=None)
                sessions = defaultdict(list)
                for bar in read_15m(db, symbol, start, finish):
                    sessions[bar.time.date()].append(bar)
                result = replay(candidate, sessions, cost_bps=20.0, config=config,
                                entry_cutoff=time(12, 30))
                if not result or result["status"] != "trade":
                    continue
                risk_pct = 100 * (result["entry"] / result["stop"] - 1)
                if not .75 <= risk_pct < 2.0:
                    continue
                output.append(dict(symbol=symbol, side="EQ",
                    entry_time=_stamp(result["entry_time"]), exit_time=_stamp(result["exit_time"]),
                    entry=Decimal(str(result["entry"])), exit=Decimal(str(result["exit"])),
                    points=Decimal(str(result["exit"])) - Decimal(str(result["entry"])),
                    stop=Decimal(str(result["stop"])), target=Decimal(str(
                        result["trigger"] + 2 * (result["trigger"] - result["stop"]))),
                    reason=result["exit_reason"],
                    entry_reason="gap_2pct_prior4_volume_1.25x_opening15m_confirmed"))
        return output

    if key == "rsi_highway":
        from backtest.rsi_highway import breadth_by_day, load_daily_candles, run_symbol
        data = load_daily_candles(symbols, end - timedelta(days=900), end,
                                  database_only=True, skip_missing_instruments=True, now=now)
        breadth = breadth_by_day(data)
        output = []
        for symbol, rows in data.items():
            cycles, _ = run_symbol(symbol, rows, breadth, breadth_minimum=.20, cost_bps=10.0)
            for row in cycles:
                entry_day, exit_day = date.fromisoformat(row["entry"]), date.fromisoformat(row["exit"])
                if entry_day not in keep_dates:
                    continue
                entry = next(c.open for c in rows if c.day == entry_day)
                exit_price = next(c.open for c in rows if c.day == exit_day)
                output.append(_equity_trade(symbol, entry_day, exit_day, entry, exit_price,
                                             "rsi_exit", entry_reason="rsi_highway_close_confirmed"))
        return output

    from backtest.smallcap_momentum_swing import load_database_data, summarize
    from backtest.smallcap_momentum_intraday_confirmation_research import read_bars
    from strategies.smallcap_momentum_swing import confirm_strong_start
    from statistics import median
    from trading.database import get_db
    data = load_database_data(symbols, end - timedelta(days=550), end,
                              database_only=True, skip_missing_instruments=True, now=now)
    baseline = summarize(data, requested_symbols=len(symbols),
                         universe_names=("nifty500", "midcap150", "smallcap500"))
    output = []
    with get_db() as db:
        for row in baseline["trade_log"]:
            entry_day = date.fromisoformat(row["entry"])
            if entry_day not in keep_dates:
                continue
            symbol = row["symbol"]
            dated = data.get(symbol, [])
            index = next((i for i, (day, _) in enumerate(dated) if day == entry_day), None)
            if not index:
                continue
            setup = dated[index - 1][1]
            start = datetime.combine(entry_day - timedelta(days=45), time.min, IST).astimezone(timezone.utc).replace(tzinfo=None)
            finish = datetime.combine(entry_day + timedelta(days=1), time.min, IST).astimezone(timezone.utc).replace(tzinfo=None)
            bars = read_bars(db, symbol, start, finish)
            sessions = {}
            for bar in bars:
                sessions.setdefault(bar[0].date(), []).append(bar)
            opening = sorted(sessions.get(entry_day, []), key=lambda item: item[0])
            prior = sorted(day for day in sessions if day < entry_day and sessions[day])[-20:]
            if not opening or len(prior) < 10:
                continue
            first = opening[0]
            prior_volumes = [sorted(sessions[day], key=lambda item: item[0])[0][5] for day in prior]
            typical = median(prior_volumes)
            if typical <= 0:
                continue
            from strategies.smallcap_momentum_swing import Candle
            bar = Candle(first[1], first[2], first[3], first[4], first[5])
            decision = confirm_strong_start(setup, [bar], same_time_volume_ratio=first[5] / typical)
            if decision.action.value != "ENTER":
                continue
            output.append(_equity_trade(symbol, entry_day, date.fromisoformat(row["exit"]),
                                         first[4], row["exit_price"], row["exit_reason"],
                                         stop=decision.initial_stop,
                                         entry_reason="v2_first_15m_upper30_rvol_confirmed"))
    return output


def persist_equity(factory, user_id, key, trades, now):
    """Persist idempotent paper equity trades in the shared strategy journal."""
    from sqlalchemy import select
    from trading.strategy_records import StrategyRun, StrategyTrade
    with factory() as db:
        missing = [trade for trade in trades if not db.scalar(select(StrategyTrade.id).where(
            StrategyTrade.user_id == user_id, StrategyTrade.strategy_key == key,
            StrategyTrade.option_symbol == trade["symbol"],
            StrategyTrade.entry_signal_time == trade["entry_time"]).limit(1))]
        if not missing:
            return 0
        run = StrategyRun(user_id=user_id, strategy_key=key, strategy_version=VERSIONS[key],
                          mode="paper", status="completed", started_at=now, ended_at=now,
                          heartbeat_at=now, configuration={"day_end_backfill": True,
                          "universe": ["nifty500", "midcap150", "smallcap500"],
                          "database_only": True})
        db.add(run); db.flush()
        for number, trade in enumerate(missing, 1):
            db.add(StrategyTrade(strategy_run_id=run.id, user_id=user_id, strategy_key=key,
                strategy_version=VERSIONS[key], mode="paper", trading_date=trade["entry_time"].date(),
                trade_number=number, status="closed", side="EQ", option_symbol=trade["symbol"],
                option_expiry=trade["exit_time"].date(), option_strike=trade["entry"], lot_count=1,
                quantity=1, entry_signal_time=trade["entry_time"], exit_signal_time=trade["exit_time"],
                entry_confirmed_at=trade["entry_time"], exit_confirmed_at=trade["exit_time"],
                entry_reason=trade["entry_reason"], exit_reason=trade["reason"],
                entry_index=trade["entry"], exit_index=trade["exit"], initial_stop=trade["stop"],
                current_stop=trade["stop"], target=trade["target"], index_points=trade["points"],
                error_message="Paper equity replay from database market snapshots; no broker order."))
        return len(missing)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--sessions", type=int, default=10, choices=(10,), help="Fixed safety window")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--only", choices=tuple(VERSIONS))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from nifty_15m_env import load_nifty_environment
    load_nifty_environment()
    from trading.database import get_db
    from trading.user_token import fetch_user_token
    from sqlalchemy import text
    with get_db() as db:
        observations = db.execute(text("SELECT strategy_key, details FROM strategy_service_observations WHERE user_id=:uid"), {"uid": args.user_id}).all()
    active = ([args.only] if args.only else sorted(
        {key for key, details in observations if key in VERSIONS and is_active(details)} |
        RESEARCH_REPLAY_KEYS))
    if not active:
        log.info("No active strategies; nothing to backfill")
        return
    today = datetime.now(IST).date()
    equity_only = args.only in EQUITY_REPLAY_KEYS
    if equity_only:
        user_id = args.user_id
        with get_db() as db:
            rows = db.execute(text("""SELECT DISTINCT captured_at::date AS session
                FROM market_snapshots WHERE timeframe='1d' AND captured_at::date <= :today
                ORDER BY session DESC LIMIT :count"""),
                {"today": today, "count": args.sessions}).all()
        sessions = {row[0] for row in rows}
        minute = fifteen = daily = []
    else:
        kite, user_id = fetch_user_token(log, user_id=args.user_id)
        nifty = next(item for item in kite.instruments("NSE") if item["tradingsymbol"] == "NIFTY 50")
        nfo_instruments = kite.instruments("NFO")
        start = today - timedelta(days=120)
        daily = _fetch(kite, nifty["instrument_token"], start, today, "day")
        sessions = set(recent_session_dates(daily, args.sessions))
        intraday_start = min(sessions) - timedelta(days=35)  # EMA warm-up.
        fifteen = _fetch(kite, nifty["instrument_token"], intraday_start, today, "15minute")
        minute = _fetch(kite, nifty["instrument_token"], min(sessions), today, "minute")
    if not sessions:
        log.info("No completed database sessions; nothing to backfill")
        return
    total = 0
    for key in active:
        if key in EQUITY_REPLAY_KEYS:
            trades = replay_equity(key, sessions, now=datetime.now(IST))
            count = 0 if args.dry_run else persist_equity(get_db, user_id, key, trades, datetime.now(timezone.utc))
        else:
            trades = replay(key, minute, fifteen, daily, sessions,
                            kite=kite, nfo_instruments=nfo_instruments)
            count = 0 if args.dry_run else persist(get_db, user_id, key, trades, datetime.now(timezone.utc))
        total += count
        log.info("%s: replayed=%d inserted=%d", key, len(trades), count)
    log.info("Day-end backfill complete: inserted=%d active_strategies=%d", total, len(active))


if __name__ == "__main__":
    main()
