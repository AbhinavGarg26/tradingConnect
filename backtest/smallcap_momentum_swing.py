"""Daily-bar research proxy for the Smallcap Momentum Swing strategy.

Reads completed NSE Smallcap 500 candles and volume from market_snapshots/1d.
The entry proxy uses a next-session gap above the setup-day high; daily data
cannot verify the video-specific first-5-to-15-minute hold and volume test.
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from collections import Counter
from dataclasses import asdict
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

from strategies.smallcap_momentum_swing import Action, Candle, Config, evaluate_setup, scan_for_focus

IST = ZoneInfo("Asia/Kolkata")


def load_database_data(symbols, start: date, end: date, *, session_factory=None,
                       kite_factory=None, database_only=False,
                       skip_missing_instruments=False, now=None):
    """Read cached OHLCV, fetch missing Kite days, and persist the repairs."""
    from sqlalchemy import text
    from backtest.session_calendar import regular_session_closed
    from backtest.snapshot_cache import valid_ohlc
    if session_factory is None:
        from nifty_15m_env import load_nifty_environment
        load_nifty_environment()
        from trading.database import get_db
        session_factory = get_db
    now = now or datetime.now(IST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    now = now.astimezone(IST)
    close_time = datetime_time(15, 30)
    last_complete = min(end, now.date() if now.time() >= close_time else now.date() - timedelta(days=1))
    if last_complete < start:
        return {symbol: [] for symbol in symbols}

    kite = None
    catalog = {}
    result = {}
    with session_factory() as db:
        try:
            holiday_rows = db.execute(text("""
                SELECT holiday_date FROM trading_holidays
                WHERE holiday_date BETWEEN :start AND :end
            """), {"start": start, "end": last_complete})
            holidays = {date.fromisoformat(row[0]) if isinstance(row[0], str) else row[0]
                        for row in holiday_rows}
        except Exception:
            db.rollback()
            holidays = set()
        expected = set()
        cursor = start
        while cursor <= last_complete:
            if not regular_session_closed(cursor) and cursor not in holidays:
                expected.add(cursor)
            cursor += timedelta(days=1)

        for symbol in symbols:
            def read_saved():
                records = db.execute(text("""
                    SELECT captured_at, open_price, high_price, low_price, close_price, volume
                    FROM market_snapshots
                    WHERE symbol = :symbol AND timeframe = '1d'
                      AND captured_at >= :start AND captured_at < :end
                    ORDER BY captured_at
                """), {
                    "symbol": symbol, "start": datetime.combine(start, datetime_time.min),
                    "end": datetime.combine(last_complete + timedelta(days=1), datetime_time.min),
                })
                saved = {}
                for stamp, op, high, low, close, volume in records:
                    if isinstance(stamp, str):
                        stamp = datetime.fromisoformat(stamp)
                    row = {"open": op, "high": high, "low": low, "close": close}
                    candle_day = stamp.date()
                    if candle_day in expected and valid_ohlc(row) and volume is not None and float(volume) >= 0:
                        saved[candle_day] = Candle(*(float(row[key]) for key in ("open", "high", "low", "close")),
                                                   float(volume))
                return saved

            saved = read_saved()
            # Volume is indispensable to this strategy. Treat legacy OHLC-only
            # rows with zero volume as gaps so Kite repairs them.
            missing = expected - {day for day, candle in saved.items() if candle.volume > 0}
            if missing and not database_only:
                if kite is None:
                    if kite_factory is None:
                        from trading.user_token import fetch_user_token
                        kite, _ = fetch_user_token(logging.getLogger(__name__), user_id=975447485)
                    else:
                        kite = kite_factory()
                    catalog = {item["tradingsymbol"]: item["instrument_token"]
                               for item in kite.instruments("NSE")
                               if str(item.get("instrument_type", "")).upper() == "EQ"}
                token = catalog.get(symbol)
                if token is None:
                    if skip_missing_instruments:
                        result[symbol] = []
                        continue
                    raise ValueError(f"NSE equity instrument missing: {symbol}")
                fetched = {}
                fetch_cursor = min(missing)
                while fetch_cursor <= max(missing):
                    finish = min(fetch_cursor + timedelta(days=1499), max(missing))
                    for item in kite.historical_data(token, fetch_cursor, finish, "day"):
                        stamp = item["date"]
                        candle_day = (stamp.astimezone(IST).date()
                                      if isinstance(stamp, datetime) and stamp.tzinfo else
                                      stamp.date() if isinstance(stamp, datetime) else stamp)
                        if candle_day in missing and valid_ohlc(item) and float(item.get("volume") or 0) > 0:
                            fetched[candle_day] = item
                    fetch_cursor = finish + timedelta(days=1)
                    time.sleep(.35)
                stamp_now = now.astimezone(timezone.utc).replace(tzinfo=None)
                insert_rows = [{
                    "symbol": symbol,
                    "captured": datetime.combine(candle_day, close_time, IST).astimezone(timezone.utc).replace(tzinfo=None),
                    "open": item["open"], "high": item["high"], "low": item["low"],
                    "close": item["close"], "volume": int(item["volume"]), "now": stamp_now,
                } for candle_day, item in fetched.items()]
                for offset in range(0, len(insert_rows), 200):
                    chunk = insert_rows[offset:offset + 200]
                    params, placeholders = {}, []
                    for row_index, row in enumerate(chunk):
                        keys = {key: f"{key}_{row_index}" for key in row}
                        placeholders.append(
                            f"(:{keys['symbol']}, '1d', :{keys['captured']}, :{keys['close']}, "
                            f":{keys['open']}, :{keys['high']}, :{keys['low']}, :{keys['close']}, "
                            f":{keys['volume']}, :{keys['now']}, :{keys['now']})")
                        params.update({keys[key]: value for key, value in row.items()})
                    db.execute(text(f"""
                        INSERT INTO market_snapshots
                          (symbol, timeframe, captured_at, ltp, open_price, high_price,
                           low_price, close_price, volume, created_at, updated_at)
                        VALUES {', '.join(placeholders)}
                        ON CONFLICT (symbol, timeframe, captured_at) DO UPDATE SET
                          ltp = EXCLUDED.ltp, open_price = EXCLUDED.open_price,
                          high_price = EXCLUDED.high_price, low_price = EXCLUDED.low_price,
                          close_price = EXCLUDED.close_price, volume = EXCLUDED.volume,
                          updated_at = EXCLUDED.updated_at
                    """), params)
                db.commit()
                saved = read_saved()
            result[symbol] = [(day, saved[day]) for day in sorted(saved)
                              if day in expected and saved[day].volume > 0]
    return result


def sma(rows, end_index: int, period: int):
    if end_index + 1 < period:
        return None
    return mean(row.close for _, row in rows[end_index - period + 1:end_index + 1])


def run_symbol(symbol: str, dated_rows, *, config=Config(), cost_bps=20.0):
    """Replay one symbol, filling only at the next session open or stop price."""
    rows = dated_rows
    trades, events = [], []
    focus_index = None
    setup_index = None
    position = None

    for i, (day, bar) in enumerate(rows):
        history = [item for _, item in rows[:i + 1]]
        if i >= config.scan_lookback:
            scan = scan_for_focus(history, config)
            if scan.action == Action.SCAN:
                focus_index = i
                events.append({"date": str(day), "type": "SCAN", "reason": scan.reason})

        if position:
            stop = position["stop"]
            # Gap-through stops fill at the open; otherwise assume a stop fill.
            if bar.open <= stop or bar.low <= stop:
                exit_price = min(bar.open, stop)
                position["exit_day"] = day
                position["exit_price"] = exit_price
                position["exit_reason"] = "protective_stop"
                position["realized_return"] += position["remaining"] * (exit_price / position["entry_price"] - 1)
                position["remaining"] = 0.0
            else:
                impulse_base = position["impulse_base"]
                move_pct = 100 * (bar.close / impulse_base - 1)
                gain_pct = 100 * (bar.close / position["entry_price"] - 1)
                if move_pct >= config.full_exit_move_pct:
                    position["exit_day"] = day
                    position["exit_price"] = bar.close
                    position["exit_reason"] = "mature_impulse_extension"
                    position["realized_return"] += position["remaining"] * (bar.close / position["entry_price"] - 1)
                    position["remaining"] = 0.0
                else:
                    if move_pct >= config.scale_out_move_pct and not position["scaled_out"]:
                        fraction = min(.60, position["remaining"])
                        position["realized_return"] += fraction * (bar.close / position["entry_price"] - 1)
                        position["remaining"] -= fraction
                        position["scaled_out"] = True
                        events.append({"date": str(day), "type": "PARTIAL_EXIT", "symbol": symbol,
                                       "fraction": fraction, "price": bar.close})
                    trail = sma(rows, i, 20 if move_pct < 40 else 10)
                    candidates = [stop]
                    if gain_pct >= config.break_even_profit_pct:
                        candidates.append(position["entry_price"])
                    if trail is not None:
                        candidates.append(trail)
                    position["stop"] = max(candidates)
                    position["last_mark"] = bar.close
            if position["remaining"] <= 0:
                gross = position["realized_return"]
                # Approximate round-trip costs on total traded notional.
                net = gross - (position["filled_fraction"] * 2 * cost_bps / 10_000)
                trades.append({"symbol": symbol, "entry": str(position["entry_day"]),
                               "exit": str(position["exit_day"]), "entry_price": position["entry_price"],
                               "exit_price": position.get("exit_price", bar.close),
                               "holding_days": (position["exit_day"] - position["entry_day"]).days,
                               "gross_return_pct": round(gross * 100, 3), "net_return_pct": round(net * 100, 3),
                               "exit_reason": position["exit_reason"], "scaled_out": position["scaled_out"]})
                position = None

        if position is None and setup_index is not None and i == setup_index + 1:
            setup_day, setup_bar = rows[setup_index]
            # Strict gap above prior high is a reproducible daily proxy for a
            # strong start. The original intraday volume/hold checks are absent.
            if bar.open > setup_bar.high:
                trigger = bar.open
                initial_stop = setup_bar.low
                if initial_stop < trigger:
                    base_rows = [r for _, r in rows[max(0, setup_index - config.impulse_lookback + 1):setup_index + 1]]
                    base = min(r.low for r in base_rows)
                    if bar.low <= initial_stop:
                        # With daily data, assume the setup-low stop was hit on
                        # entry day if the day's low crossed it.
                        gross = initial_stop / trigger - 1
                        net = gross - 2 * cost_bps / 10_000
                        trades.append({"symbol": symbol, "entry": str(day), "exit": str(day),
                                       "entry_price": trigger, "exit_price": initial_stop,
                                       "holding_days": 0, "gross_return_pct": round(gross * 100, 3),
                                       "net_return_pct": round(net * 100, 3),
                                       "exit_reason": "entry_session_stop_daily_bar_proxy", "scaled_out": False})
                    else:
                        position = {"entry_day": day, "entry_price": trigger, "stop": initial_stop,
                                    "impulse_base": base, "remaining": 1.0, "filled_fraction": 1.0,
                                    "realized_return": 0.0, "scaled_out": False, "last_mark": trigger}
                        events.append({"date": str(day), "type": "ENTRY", "symbol": symbol,
                                       "price": trigger, "stop": initial_stop,
                                       "proxy": "gap_above_setup_high; intraday confirmation unavailable"})
            setup_index = None

        if position is None and i >= config.impulse_lookback - 1:
            focused = focus_index is not None and i - focus_index <= 180
            setup = evaluate_setup(history, previously_scanned=focused, config=config)
            if setup.action == Action.WATCH and i + 1 < len(rows):
                setup_index = i
                events.append({"date": str(day), "type": "WATCH", "reason": setup.reason})

    open_position = None
    if position:
        last_day, last_bar = rows[-1]
        unrealized = position["realized_return"] + position["remaining"] * (
            last_bar.close / position["entry_price"] - 1)
        open_position = {"symbol": symbol, "entry": str(position["entry_day"]),
                         "mark_return_pct": round(unrealized * 100, 3), "stop": position["stop"]}
    return trades, events, open_position


def summarize(data, *, config=Config(), cost_bps=20.0, requested_symbols=0,
              universe_names=("smallcap500",)):
    all_trades, all_events, open_positions = [], [], []
    counts = {}
    for symbol, rows in data.items():
        counts[symbol] = len(rows)
        if rows:
            trades, events, open_position = run_symbol(symbol, rows, config=config, cost_bps=cost_bps)
            all_trades.extend(trades)
            all_events.extend(events)
            if open_position:
                open_positions.append(open_position)
    all_trades.sort(key=lambda row: (row["entry"], row["symbol"]))
    returns = [row["net_return_pct"] for row in all_trades]
    years = Counter(row["entry"][:4] for row in all_trades)
    observed = [day for rows in data.values() for day, _ in rows]
    return {
        "strategy": "smallcap_momentum_swing", "version": "v1-volume-pullback-strong-start",
        "universe": ("current " + " + ".join(universe_names)
                     + " constituent lists (deduplicated; survivorship-biased)"),
        "requested_symbols": requested_symbols, "symbols_with_data": sum(bool(v) for v in data.values()),
        "candles": sum(counts.values()), "period": [str(min(observed)), str(max(observed))] if observed else None,
        "assumptions": {
            "rules": asdict(config), "execution": "next-session open only when it gaps above setup-day high",
            "entry_proxy_limit": "daily OHLCV cannot verify first 5-15 minute hold or same-time volume confirmation",
            "stops": "setup-day low as initial stop; gap-through stop fills at open; otherwise stop-price fill",
            "cost": f"{cost_bps} bps per side on traded notional; excludes taxes and market impact",
            "sizing": "one equal-notional unit per entry; returns are per-trade, not portfolio-level capital simulation",
            "data": "market_snapshots / 1d; current constituents create survivorship bias",
        },
        "scan_events": sum(e["type"] == "SCAN" for e in all_events),
        "watch_setups": sum(e["type"] == "WATCH" for e in all_events),
        "trades": len(all_trades),
        "win_rate_pct": round(100 * sum(value > 0 for value in returns) / len(returns), 2) if returns else None,
        "average_net_trade_pct": round(mean(returns), 3) if returns else None,
        "median_net_trade_pct": sorted(returns)[len(returns)//2] if returns else None,
        "sum_net_trade_pct": round(sum(returns), 3),
        "trades_by_entry_year": dict(sorted(years.items())),
        "open_positions": open_positions,
        "data_coverage": {"symbols_without_rows": [key for key, value in counts.items() if value == 0],
                          "rows_per_symbol_min": min(counts.values()) if counts else 0,
                          "rows_per_symbol_median": sorted(counts.values())[len(counts)//2] if counts else 0,
                          "rows_per_symbol_max": max(counts.values()) if counts else 0},
        "trade_log": all_trades,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2020, 1, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 11, 14))
    parser.add_argument("--cost-bps", type=float, default=20.0)
    parser.add_argument("--database-only", action="store_true",
                        help="use only stored daily OHLCV and do not fetch Kite gaps")
    parser.add_argument("--skip-instrument-sync", action="store_true")
    parser.add_argument("--refresh-universe", action="store_true")
    parser.add_argument("--indexes", nargs="+", default=["smallcap500"],
                        choices=["nifty500", "midcap150", "smallcap500"],
                        help="current constituent universes to combine and deduplicate")
    parser.add_argument("--output", type=Path, default=Path("outputs/smallcap_momentum_swing_backtest.json"))
    args = parser.parse_args()
    from backtest.equity_universe import load_equity_universe, sync_equity_instruments
    symbols = load_equity_universe(tuple(args.indexes), refresh=args.refresh_universe)
    kite = None
    if not args.database_only or not args.skip_instrument_sync:
        from nifty_15m_env import load_nifty_environment
        load_nifty_environment()
        from trading.user_token import fetch_user_token
        kite, _ = fetch_user_token(logging.getLogger(__name__), user_id=975447485)
    if not args.skip_instrument_sync:
        from trading.database import get_db
        with get_db() as db:
            sync_result = sync_equity_instruments(db, kite, symbols)
        print(json.dumps({"instrument_sync": sync_result}))
    try:
        data = load_database_data(
            symbols, args.start, args.end,
            kite_factory=None if args.database_only else lambda: kite,
            database_only=args.database_only, skip_missing_instruments=True,
        )
    except Exception as exc:
        parser.error(f"Cannot read market_snapshots from the configured database ({type(exc).__name__}). "
                     "Start the local PostgreSQL/SSH tunnel, then rerun. No backtest report was produced.")
    report = summarize(data, cost_bps=args.cost_bps, requested_symbols=len(symbols),
                       universe_names=tuple(args.indexes))
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2))
    print(json.dumps({key: value for key, value in report.items() if key != "trade_log"}, indent=2))
    print(f"Trade log saved to {path}")


if __name__ == "__main__":
    main()
