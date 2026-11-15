"""Sampled 15-minute validation of Smallcap Momentum opening confirmation.

Fetches and persists only the sampled trade windows.  The result is a
correlation screen on baseline trade outcomes, not a full intraday execution
simulation; it identifies whether the intended opening confirmation deserves
a full-universe intraday backtest.
"""
from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from statistics import mean, median
from zoneinfo import ZoneInfo

from sqlalchemy import text

from backtest.smallcap_momentum_swing import load_database_data


IST = ZoneInfo("Asia/Kolkata")


def evenly_sample(rows, size):
    if len(rows) <= size:
        return rows
    return [rows[round(index * (len(rows) - 1) / (size - 1))] for index in range(size)]


def persist_bars(db, symbol, bars):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = []
    for bar in bars:
        stamp = bar["date"]
        if stamp.tzinfo:
            stamp = stamp.astimezone(timezone.utc).replace(tzinfo=None)
        volume = int(bar.get("volume") or 0)
        # Kite occasionally returns INT64_MAX as a missing-volume sentinel for
        # old, illiquid candles. It is not a traded quantity and exceeds the
        # market_snapshots numeric column, so retain the price bar with volume 0.
        if volume >= 10**15:
            volume = 0
        rows.append({"symbol": symbol, "captured": stamp, "open": bar["open"],
                     "high": bar["high"], "low": bar["low"], "close": bar["close"],
                     "volume": volume, "now": now})
    for offset in range(0, len(rows), 200):
        chunk = rows[offset:offset + 200]
        params, placeholders = {}, []
        for index, row in enumerate(chunk):
            keys = {key: f"{key}_{index}" for key in row}
            placeholders.append(
                f"(:{keys['symbol']}, '15m', :{keys['captured']}, :{keys['close']}, "
                f":{keys['open']}, :{keys['high']}, :{keys['low']}, :{keys['close']}, "
                f":{keys['volume']}, :{keys['now']}, :{keys['now']})"
            )
            params.update({keys[key]: value for key, value in row.items()})
        db.execute(text(f"""
            INSERT INTO market_snapshots
              (symbol, timeframe, captured_at, ltp, open_price, high_price,
               low_price, close_price, volume, created_at, updated_at)
            VALUES {', '.join(placeholders)}
            ON CONFLICT (symbol, timeframe, captured_at) DO UPDATE SET
              ltp=EXCLUDED.ltp, open_price=EXCLUDED.open_price,
              high_price=EXCLUDED.high_price, low_price=EXCLUDED.low_price,
              close_price=EXCLUDED.close_price, volume=EXCLUDED.volume,
              updated_at=EXCLUDED.updated_at
        """), params)


def read_bars(db, symbol, start, end):
    values = db.execute(text("""
        SELECT captured_at, open_price, high_price, low_price, close_price, volume
        FROM market_snapshots
        WHERE symbol=:symbol AND timeframe='15m'
          AND captured_at >= :start AND captured_at < :end
        ORDER BY captured_at
    """), {"symbol": symbol, "start": start, "end": end})
    result = []
    for stamp, op, high, low, close, volume in values:
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        result.append((stamp.astimezone(IST), float(op), float(high), float(low),
                       float(close), float(volume or 0)))
    return result


def result_metrics(rows):
    values = [row["net_return_pct"] for row in rows]
    wins = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value <= 0)
    return {"trades": len(rows),
            "win_rate_pct": round(100 * sum(value > 0 for value in values) / len(values), 2) if values else None,
            "average_net_trade_pct": round(mean(values), 4) if values else None,
            "profit_factor": round(wins / losses, 4) if losses else None,
            "entry_session_stop_pct": round(100 * sum(
                row["exit_reason"] == "entry_session_stop_daily_bar_proxy" for row in rows
            ) / len(rows), 2) if rows else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sample-per-year", type=int, default=40)
    parser.add_argument("--fetch", action="store_true")
    args = parser.parse_args()
    trades = json.loads(args.baseline.read_text())["trade_log"]
    by_year = defaultdict(list)
    for trade in trades:
        by_year[trade["entry"][:4]].append(trade)
    sample = []
    for year in sorted(by_year):
        sample.extend(evenly_sample(by_year[year], args.sample_per_year))

    from nifty_15m_env import load_nifty_environment
    load_nifty_environment()
    from trading.database import get_db
    kite = None
    catalog = None
    if args.fetch:
        from trading.user_token import fetch_user_token
        kite, _ = fetch_user_token(__import__("logging").getLogger(__name__), user_id=975447485)
        catalog = {row["tradingsymbol"]: row["instrument_token"]
                   for row in kite.instruments("NSE") if row.get("instrument_type") == "EQ"}

    symbols = tuple(sorted({trade["symbol"] for trade in sample}))
    daily = load_database_data(symbols, date(2019, 11, 1), date(2026, 11, 14),
                               database_only=True, skip_missing_instruments=True)
    daily_indexes = {symbol: {str(day): index for index, (day, _) in enumerate(rows)}
                     for symbol, rows in daily.items()}
    evaluated = []
    with get_db() as db:
        for sequence, trade in enumerate(sample, 1):
            symbol = trade["symbol"]
            entry_day = date.fromisoformat(trade["entry"])
            start_day = entry_day - timedelta(days=45)
            start_stamp = datetime.combine(start_day, dt_time.min, IST).astimezone(timezone.utc).replace(tzinfo=None)
            end_stamp = datetime.combine(entry_day + timedelta(days=1), dt_time.min, IST).astimezone(timezone.utc).replace(tzinfo=None)
            bars = read_bars(db, symbol, start_stamp, end_stamp)
            grouped = defaultdict(list)
            for bar in bars:
                grouped[bar[0].date()].append(bar)
            if args.fetch and (entry_day not in grouped or len(grouped) < 10):
                token = catalog.get(symbol)
                if token:
                    fetched = kite.historical_data(token, start_day, entry_day, "15minute")
                    persist_bars(db, symbol, fetched)
                    db.commit()
                    bars = read_bars(db, symbol, start_stamp, end_stamp)
                    grouped = defaultdict(list)
                    for bar in bars:
                        grouped[bar[0].date()].append(bar)
                    time.sleep(.35)
            sessions = sorted(day for day in grouped if day < entry_day and grouped[day])
            opening = sorted(grouped.get(entry_day, []), key=lambda row: row[0])
            if not opening or len(sessions) < 10:
                continue
            daily_index = daily_indexes.get(symbol, {}).get(str(entry_day))
            if daily_index is None or daily_index == 0:
                continue
            setup = daily[symbol][daily_index - 1][1]
            first = opening[0]
            historical_open_volumes = [sorted(grouped[day], key=lambda row: row[0])[0][5]
                                        for day in sessions[-20:]]
            volume_ratio = first[5] / median(historical_open_volumes) if median(historical_open_volumes) else 0
            location = (first[4] - first[3]) / (first[2] - first[3]) if first[2] > first[3] else 0
            confirmed = (first[3] >= setup.low and first[4] > setup.high
                         and volume_ratio >= 1.5 and location >= .5)
            evaluated.append({**trade, "confirmed": confirmed,
                              "opening_volume_ratio": round(volume_ratio, 3),
                              "opening_close_location": round(location, 3),
                              "opening_held_setup_low": first[3] >= setup.low,
                              "opening_closed_above_setup_high": first[4] > setup.high})

    confirmed = [row for row in evaluated if row["confirmed"]]
    rejected = [row for row in evaluated if not row["confirmed"]]
    payload = {"sample_requested": len(sample), "sample_evaluated": len(evaluated),
               "confirmation": "first 15m holds setup low, closes above setup high, closes in upper half, RVOL >=1.5",
               "confirmed": result_metrics(confirmed), "rejected": result_metrics(rejected),
               "confirmed_by_year": {year: result_metrics([row for row in confirmed if row["entry"].startswith(year)])
                                     for year in sorted(by_year)},
               "evaluated_trades": evaluated}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2))
    print(json.dumps({key: value for key, value in payload.items() if key != "evaluated_trades"}, indent=2))


if __name__ == "__main__":
    main()
