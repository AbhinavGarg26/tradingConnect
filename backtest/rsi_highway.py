"""Backtest RSI Highway on NSE index universes using database-cached Kite data."""
from __future__ import annotations

import argparse
import bisect
import json
import time
from collections import defaultdict
from dataclasses import asdict
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

from strategies.rsi_highway import Action, Candle, Config, State, evaluate_values, rsi


IST = ZoneInfo("Asia/Kolkata")


# NIFTY 50 top ten by free-float weight in the 27-Feb-2026 NSE Indices
# whitepaper. This fixed present-day universe creates survivorship bias.
DEFAULT_SYMBOLS = (
    "HDFCBANK", "ICICIBANK", "RELIANCE", "BHARTIARTL", "LT",
    "SBIN", "INFY", "AXISBANK", "KOTAKBANK", "M&M",
)

DEFAULT_UNIVERSE_NAMES = ("nifty500", "midcap150", "smallcap500")


def aggregate(candles: list[Candle], frequency: str) -> list[Candle]:
    groups: dict[tuple[int, ...], list[Candle]] = defaultdict(list)
    for candle in candles:
        if frequency == "month":
            key = (candle.day.year, candle.day.month)
        else:
            iso = candle.day.isocalendar()
            key = (iso.year, iso.week)
        groups[key].append(candle)
    return [Candle(rows[-1].day, rows[0].open, max(row.high for row in rows),
                   min(row.low for row in rows), rows[-1].close)
            for _, rows in sorted(groups.items())]


def completed_periods(candles: list[Candle], current: date, frequency: str) -> list[Candle]:
    values = aggregate([candle for candle in candles if candle.day < current], frequency)
    if frequency == "month":
        return [value for value in values if (value.day.year, value.day.month) != (current.year, current.month)]
    current_week = current.isocalendar()[:2]
    return [value for value in values if value.day.isocalendar()[:2] != current_week]


def breadth_by_day(data: dict[str, list[Candle]], minimum_history=252) -> dict[date, float]:
    histories = {symbol: {candle.day: candle for candle in rows} for symbol, rows in data.items()}
    days = sorted({day for values in histories.values() for day in values})
    closes: dict[str, list[float]] = defaultdict(list)
    result = {}
    for day in days:
        eligible = at_high = 0
        for symbol, values in histories.items():
            candle = values.get(day)
            if candle is None:
                continue
            closes[symbol].append(candle.close)
            if len(closes[symbol]) < minimum_history:
                continue
            eligible += 1
            if candle.close >= max(closes[symbol][-minimum_history:]):
                at_high += 1
        if eligible:
            result[day] = at_high / eligible
    return result


def run_symbol(symbol: str, rows: list[Candle], breadth: dict[date, float], *,
               breadth_minimum: float, cost_bps: float, config=Config()):
    if len(rows) < config.rsi_period + 2:
        return [], None
    state = State()
    lots: list[tuple[date, float]] = []
    cycles = []
    pending = None
    daily_rsi = rsi([row.close for row in rows], config.rsi_period)
    monthly = aggregate(rows, "month")
    weekly = aggregate(rows, "week")
    monthly_rsi = rsi([row.close for row in monthly], config.rsi_period)
    weekly_rsi = rsi([row.close for row in weekly], config.rsi_period)
    month_keys = [(row.day.year, row.day.month) for row in monthly]
    week_keys = [row.day.isocalendar()[:2] for row in weekly]

    for index, current in enumerate(rows):
        if pending:
            action, signal_day = pending
            if action in {Action.ENTER, Action.ADD}:
                lots.append((current.day, current.open))
            elif action == Action.EXIT and lots:
                lot_returns = [current.open / entry - 1 for _, entry in lots]
                gross = sum(lot_returns) / config.max_entries
                costs = len(lots) * 2 * cost_bps / 10_000 / config.max_entries
                cycles.append({
                    "symbol": symbol, "entry": str(lots[0][0]), "exit": str(current.day),
                    "entries": len(lots), "holding_days": (current.day - lots[0][0]).days,
                    "gross_return": gross, "net_return": gross - costs,
                    "exit_signal_day": str(signal_day),
                })
                lots = []
            pending = None

        month_index = bisect.bisect_left(month_keys, (current.day.year, current.day.month)) - 1
        week_index = bisect.bisect_left(week_keys, current.day.isocalendar()[:2]) - 1
        decision, state = evaluate_values(
            current=current, previous=rows[max(0, index - 1)],
            recent_daily=rows[max(0, index - config.swing_lookback + 1):index + 1],
            monthly_rsi=monthly_rsi[month_index] if month_index >= 0 else None,
            weekly_rsi=weekly_rsi[week_index] if week_index >= 0 else None,
            previous_weekly_rsi=weekly_rsi[week_index - 1] if week_index >= 1 else None,
            previous_daily_rsi=daily_rsi[index - 1] if index >= 1 else None,
            daily_rsi=daily_rsi[index],
            breadth_healthy=breadth.get(current.day, 0.0) >= breadth_minimum,
            state=state, config=config,
        )
        if decision.action in {Action.ENTER, Action.ADD, Action.EXIT} and index + 1 < len(rows):
            pending = (decision.action, current.day)

    open_mark = None
    if lots:
        lot_returns = [rows[-1].close / entry - 1 for _, entry in lots]
        open_mark = sum(lot_returns) / config.max_entries
    return cycles, open_mark


def summarize(data: dict[str, list[Candle]], *, breadth_minimum=.20, cost_bps=10.0,
              universe_description="fixed current top-ten NIFTY 50 weights; survivorship-biased"):
    data = {symbol: rows for symbol, rows in data.items() if rows}
    if not data:
        raise ValueError("No daily candles are available for the selected universe and dates")
    breadth = breadth_by_day(data)
    cycles = []
    open_positions = {}
    for symbol, rows in data.items():
        values, open_mark = run_symbol(symbol, rows, breadth, breadth_minimum=breadth_minimum, cost_bps=cost_bps)
        cycles.extend(values)
        if open_mark is not None:
            open_positions[symbol] = open_mark
    cycles.sort(key=lambda row: (row["exit"], row["symbol"]))
    equity = peak = 1.0
    max_drawdown = 0.0
    for cycle in cycles:
        equity *= 1 + cycle["net_return"] / len(data)
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1)
    by_symbol = {}
    for symbol in data:
        values = [row for row in cycles if row["symbol"] == symbol]
        by_symbol[symbol] = {
            "cycles": len(values),
            "win_rate_pct": round(100 * sum(row["net_return"] > 0 for row in values) / len(values), 1) if values else None,
            "net_return_pct": round(100 * (sum(row["net_return"] for row in values)), 2),
        }
    return {
        "symbols": list(data), "start": str(min(rows[0].day for rows in data.values())),
        "end": str(max(rows[-1].day for rows in data.values())),
        "assumptions": {
            "rsi": "Wilder 14; monthly/weekly >60, weekly RSI rising, entry-day RSI >=52",
            "execution": "next trading session open after a close-confirmed signal",
            "breadth": f"at least {breadth_minimum:.0%} of universe at a 252-session closing high",
            "allocation": "equal-weight stocks; each pyramid entry is one-third position",
            "cost": f"{cost_bps:.1f} bps per side per filled tranche",
            "universe": universe_description,
        },
        "cycles": len(cycles),
        "win_rate_pct": round(100 * sum(row["net_return"] > 0 for row in cycles) / len(cycles), 1) if cycles else None,
        "average_net_cycle_pct": round(100 * mean(row["net_return"] for row in cycles), 2) if cycles else None,
        "median_holding_days": sorted(row["holding_days"] for row in cycles)[len(cycles) // 2] if cycles else None,
        "equal_weight_net_return_pct": round((equity - 1) * 100, 2),
        "max_drawdown_pct": round(max_drawdown * 100, 2),
        "open_positions": {key: round(value * 100, 2) for key, value in open_positions.items()},
        "by_symbol": by_symbol,
        "trades": cycles,
    }


def fetch_kite(symbols: tuple[str, ...], start: date, end: date):
    from nifty_15m_env import load_nifty_environment
    load_nifty_environment()
    from trading.user_token import fetch_user_token
    import logging
    kite, _ = fetch_user_token(logging.getLogger(__name__), user_id=975447485)
    instruments = {item["tradingsymbol"]: item["instrument_token"] for item in kite.instruments("NSE")}
    data = {}
    for symbol in symbols:
        token = instruments.get(symbol)
        if token is None:
            raise ValueError(f"NSE instrument missing: {symbol}")
        values = []
        cursor = start
        while cursor <= end:
            finish = min(cursor + timedelta(days=1499), end)
            values.extend(kite.historical_data(token, cursor, finish, "day"))
            cursor = finish + timedelta(days=1)
            time.sleep(.35)
        data[symbol] = [Candle(
            (item["date"].astimezone(IST).date() if isinstance(item["date"], datetime) else item["date"]),
            float(item["open"]), float(item["high"]), float(item["low"]), float(item["close"]),
        ) for item in values]
    return data


def load_daily_candles(symbols: tuple[str, ...], start: date, end: date, *,
                       session_factory=None, kite_factory=None, now=None):
    """Read completed daily candles from the DB and persist any Kite gaps."""
    from sqlalchemy import text
    from backtest.session_calendar import regular_session_closed
    from backtest.snapshot_cache import valid_ohlc

    if end < start:
        raise ValueError("End precedes start")
    now = now or datetime.now(IST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    now = now.astimezone(IST)
    if session_factory is None:
        from nifty_15m_env import load_nifty_environment
        load_nifty_environment()
        from trading.database import get_db
        session_factory = get_db

    close_time = datetime_time(15, 30)
    last_complete = min(end, now.date() if now.time() >= close_time else now.date() - timedelta(days=1))
    if last_complete < start:
        return {symbol: [] for symbol in symbols}

    kite = None
    tokens = {}
    output = {}
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
        day = start
        while day <= last_complete:
            if not regular_session_closed(day) and day not in holidays:
                expected.add(day)
            day += timedelta(days=1)

        for symbol in symbols:
            def read_saved():
                records = db.execute(text("""
                    SELECT captured_at, open_price, high_price, low_price, close_price
                    FROM market_snapshots
                    WHERE symbol = :symbol AND timeframe = '1d'
                      AND captured_at >= :start AND captured_at < :end
                    ORDER BY captured_at
                """), {
                    "symbol": symbol,
                    "start": datetime.combine(start, datetime_time.min),
                    "end": datetime.combine(last_complete + timedelta(days=1), datetime_time.min),
                })
                saved = {}
                for stamp, open_price, high, low, close in records:
                    if isinstance(stamp, str):
                        stamp = datetime.fromisoformat(stamp)
                    row = {"open": open_price, "high": high, "low": low, "close": close}
                    candle_day = stamp.date()
                    if candle_day in expected and valid_ohlc(row):
                        saved[candle_day] = Candle(candle_day, *(float(row[key]) for key in
                                                               ("open", "high", "low", "close")))
                return saved

            saved = read_saved()
            missing = expected - saved.keys()
            if missing:
                if kite is None:
                    if kite_factory is None:
                        from trading.user_token import fetch_user_token
                        import logging
                        kite, _ = fetch_user_token(logging.getLogger(__name__), user_id=975447485)
                    else:
                        kite = kite_factory()
                    tokens = {item["tradingsymbol"]: item["instrument_token"]
                              for item in kite.instruments("NSE")}
                token = tokens.get(symbol)
                if token is None:
                    raise ValueError(f"NSE instrument missing: {symbol}")
                cursor = min(missing)
                fetched = {}
                while cursor <= max(missing):
                    finish = min(cursor + timedelta(days=1499), max(missing))
                    for item in kite.historical_data(token, cursor, finish, "day"):
                        stamp = item["date"]
                        candle_day = (stamp.astimezone(IST).date()
                                      if isinstance(stamp, datetime) and stamp.tzinfo else
                                      stamp.date() if isinstance(stamp, datetime) else stamp)
                        if candle_day in missing and valid_ohlc(item):
                            fetched[candle_day] = item
                    cursor = finish + timedelta(days=1)
                    time.sleep(.35)
                stamp_now = now.astimezone(timezone.utc).replace(tzinfo=None)
                for candle_day, item in fetched.items():
                    captured = datetime.combine(candle_day, close_time, IST).astimezone(timezone.utc).replace(tzinfo=None)
                    db.execute(text("""
                        INSERT INTO market_snapshots
                          (symbol, timeframe, captured_at, ltp, open_price, high_price,
                           low_price, close_price, created_at, updated_at)
                        VALUES (:symbol, '1d', :captured, :close, :open, :high,
                                :low, :close, :now, :now)
                        ON CONFLICT (symbol, timeframe, captured_at) DO NOTHING
                    """), {"symbol": symbol, "captured": captured, "open": item["open"],
                           "high": item["high"], "low": item["low"], "close": item["close"],
                           "now": stamp_now})
                db.commit()
                saved = read_saved()
            output[symbol] = [saved[day] for day in sorted(saved)]
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2020, 1, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=date.today())
    parser.add_argument("--breadth-minimum", type=float, default=.20)
    parser.add_argument("--cost-bps", type=float, default=10.0)
    parser.add_argument("--universe", choices=("top10", "broad"), default="broad",
                        help="broad = current NIFTY 500 + Midcap 150 + Smallcap 500")
    parser.add_argument("--indexes", nargs="+", choices=DEFAULT_UNIVERSE_NAMES,
                        default=list(DEFAULT_UNIVERSE_NAMES))
    parser.add_argument("--refresh-universe", action="store_true",
                        help="download fresh official constituent CSV files")
    parser.add_argument("--database-only", action="store_true",
                        help="fail if candles are absent instead of fetching them from Kite")
    parser.add_argument("--skip-instrument-sync", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.universe == "top10":
        symbols = DEFAULT_SYMBOLS
        description = "fixed current top-ten NIFTY 50 weights; survivorship-biased"
    else:
        from backtest.equity_universe import load_equity_universe, sync_equity_instruments
        symbols = load_equity_universe(args.indexes, refresh=args.refresh_universe)
        description = ("current constituent union of " + ", ".join(args.indexes) +
                       "; deduplicated and survivorship-biased")

    kite = None
    if not args.database_only or not args.skip_instrument_sync:
        from nifty_15m_env import load_nifty_environment
        load_nifty_environment()
        from trading.user_token import fetch_user_token
        import logging
        kite, _ = fetch_user_token(logging.getLogger(__name__), user_id=975447485)
    if not args.skip_instrument_sync:
        from trading.database import get_db
        with get_db() as db:
            result = sync_equity_instruments(db, kite, symbols)
        print(json.dumps({"instrument_sync": result}))
    if args.database_only:
        def no_kite():
            raise RuntimeError("Missing market_snapshots candle in --database-only mode")
        kite_factory = no_kite
    else:
        kite_factory = lambda: kite
    data = load_daily_candles(symbols, args.start, args.end, kite_factory=kite_factory)
    report = summarize(data, breadth_minimum=args.breadth_minimum,
                       cost_bps=args.cost_bps, universe_description=description)
    report["requested_symbols"] = len(symbols)
    payload = json.dumps(report, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload)
    print(payload)


if __name__ == "__main__":
    main()
