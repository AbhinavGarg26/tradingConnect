"""NIFTY 500 gap-up continuation backtest.

The replay first screens daily OHLCV and then evaluates the completed opening
15-minute bar. A catalyst CSV is required for a strategy-faithful run. The
explicit --price-only switch is available for exploratory price-action research
and is labelled as such in its report.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import time
from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from statistics import mean, median
from zoneinfo import ZoneInfo

from sqlalchemy import text

from backtest.smallcap_momentum_swing import load_database_data
from strategies.gap_up import (Action, Bar, StockGapConfig,
                               confirm_stock_continuation, stock_gap_watch)

IST = ZoneInfo("Asia/Kolkata")


def load_catalysts(path: Path | None) -> set[tuple[str, date]]:
    if path is None:
        return set()
    with path.open(newline="", encoding="utf-8-sig") as handle:
        raw_rows = csv.DictReader(handle)
        required = {"symbol", "date"}
        if not raw_rows.fieldnames or not required.issubset(
                {x.strip().lower() for x in raw_rows.fieldnames}):
            raise ValueError("Catalyst CSV must contain symbol,date columns")
        rows = ({str(key).strip().lower(): value for key, value in row.items()}
                for row in raw_rows)
        return {(str(row["symbol"]).strip().upper(), date.fromisoformat(row["date"].strip()))
                for row in rows if row.get("symbol") and row.get("date")}


def read_15m(db, symbol: str, start: datetime, end: datetime):
    rows = db.execute(text("""
        SELECT captured_at, open_price, high_price, low_price, close_price, volume
        FROM market_snapshots
        WHERE symbol=:symbol AND timeframe='15m'
          AND captured_at >= :start AND captured_at < :end
        ORDER BY captured_at
    """), {"symbol": symbol, "start": start, "end": end})
    result = []
    for stamp, op, high, low, close, volume in rows:
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        try:
            result.append(Bar(stamp.astimezone(IST), float(op), float(high), float(low),
                              float(close), float(volume or 0)))
        except (TypeError, ValueError):
            read_15m.invalid_rows += 1
    return result


read_15m.invalid_rows = 0


def persist_15m(db, symbol: str, bars):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    payload = []
    for item in bars:
        stamp = item["date"]
        if stamp.tzinfo:
            stamp = stamp.astimezone(timezone.utc).replace(tzinfo=None)
        volume = int(item.get("volume") or 0)
        if volume >= 10**15:
            volume = 0
        payload.append({"symbol": symbol, "captured": stamp, "open": item["open"],
                        "high": item["high"], "low": item["low"], "close": item["close"],
                        "volume": volume, "now": now})
    if not payload:
        return
    db.execute(text("""
        INSERT INTO market_snapshots
          (symbol, timeframe, captured_at, ltp, open_price, high_price,
           low_price, close_price, volume, created_at, updated_at)
        VALUES (:symbol, '15m', :captured, :close, :open, :high, :low, :close,
                :volume, :now, :now)
        ON CONFLICT (symbol, timeframe, captured_at) DO UPDATE SET
          ltp=EXCLUDED.ltp, open_price=EXCLUDED.open_price,
          high_price=EXCLUDED.high_price, low_price=EXCLUDED.low_price,
          close_price=EXCLUDED.close_price, volume=EXCLUDED.volume,
          updated_at=EXCLUDED.updated_at
    """), payload)


def daily_candidates(data, catalysts, *, price_only: bool, config=StockGapConfig()):
    candidates = []
    for symbol, rows in data.items():
        for index in range(20, len(rows)):
            day, current = rows[index]
            _, previous = rows[index - 1]
            prior20 = rows[index - 20][1]
            ema20 = mean(bar.close for _, bar in rows[index - 20:index])
            adv20 = mean(bar.close * bar.volume for _, bar in rows[index - 20:index])
            verified = (symbol, day) in catalysts
            decision = stock_gap_watch(
                previous_close=previous.close, previous_high=previous.high,
                today_open=current.open, prior_20d_close=prior20.close, ema20=ema20,
                has_material_catalyst=verified or price_only,
                average_traded_value=adv20, config=config,
            )
            if decision.action == Action.WATCH:
                candidates.append({"symbol": symbol, "date": day, "previous": previous,
                                   "daily": current, "verified_catalyst": verified,
                                   "screen": decision.metadata})
    return candidates


def replay(candidate, sessions, *, cost_bps: float, config=StockGapConfig(),
           entry_cutoff=dt_time(11, 30)):
    day = candidate["date"]
    today = sessions.get(day, [])
    prior_days = sorted(value for value in sessions if value < day and sessions[value])
    if len(today) < 2 or len(prior_days) < 10:
        return None
    first = today[0]
    historical = [sessions[value][0].volume for value in prior_days[-20:]
                  if sessions[value][0].volume > 0]
    if len(historical) < 10:
        return None
    rvol = first.volume / median(historical)
    # Kite candles do not expose exchange VWAP. The first-bar typical price is
    # a conservative reproducible proxy and is disclosed in the report.
    vwap_proxy = (first.high + first.low + first.close) / 3
    decision = confirm_stock_continuation(
        previous_close=candidate["previous"].close,
        previous_low=candidate["previous"].low,
        opening_15m=first, opening_relative_volume=rvol, vwap=vwap_proxy,
        market_aligned=True, sector_aligned=True, config=config,
    )
    if decision.action != Action.BUY:
        return {"status": "rejected", "reason": decision.reason}
    entry = None
    entry_time = None
    exit_price = None
    exit_time = None
    exit_reason = "eod"
    for bar in today[1:]:
        if bar.time.time() > entry_cutoff and entry is None:
            break
        if entry is None and bar.high >= decision.trigger:
            entry = max(decision.trigger, bar.open)
            entry_time = bar.time
        if entry is None:
            continue
        if bar.low <= decision.stop:
            exit_price = min(decision.stop, bar.open)
            exit_time = bar.time
            exit_reason = "stop"
            break
        if bar.high >= decision.target_2:
            exit_price = decision.target_2
            exit_time = bar.time
            exit_reason = "target_2r"
            break
    if entry is None:
        return {"status": "unfilled", "reason": "trigger_not_reached_by_1130"}
    if exit_price is None:
        eligible = [bar for bar in today if bar.time.time() <= dt_time(15, 15)]
        exit_bar = eligible[-1] if eligible else today[-1]
        exit_price = exit_bar.close
        exit_time = exit_bar.time
    gross = 100 * (exit_price / entry - 1)
    return {"status": "trade", "entry": entry, "exit": exit_price,
            "entry_time": entry_time.isoformat(), "exit_time": exit_time.isoformat(),
            "exit_reason": exit_reason, "gross_return_pct": gross,
            "net_return_pct": gross - cost_bps / 100,
            "opening_rvol": rvol, "trigger": decision.trigger, "stop": decision.stop}


def metrics(trades):
    returns = [row["net_return_pct"] for row in trades]
    gains = sum(max(value, 0) for value in returns)
    losses = -sum(min(value, 0) for value in returns)
    return {"trades": len(trades),
            "win_rate_pct": round(100 * sum(value > 0 for value in returns) / len(returns), 2) if returns else None,
            "average_net_trade_pct": round(mean(returns), 4) if returns else None,
            "profit_factor": round(gains / losses, 3) if losses else None,
            "compounded_return_pct": round(100 * (__import__("math").prod(1 + value / 100 for value in returns) - 1), 2) if returns else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2020, 1, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=date.today())
    parser.add_argument("--catalysts", type=Path)
    parser.add_argument("--price-only", action="store_true")
    parser.add_argument("--database-only", action="store_true")
    parser.add_argument("--fetch-intraday", action="store_true")
    parser.add_argument("--cost-bps", type=float, default=20.0)
    parser.add_argument("--min-gap-pct", type=float, default=2.0)
    parser.add_argument("--indexes", nargs="+",
                        choices=("nifty500", "midcap150", "smallcap500"),
                        default=["nifty500"],
                        help="current official index constituent lists to combine")
    parser.add_argument("--output", type=Path, default=Path("outputs/nifty500_gap_up_backtest.json"))
    args = parser.parse_args()
    read_15m.invalid_rows = 0
    if not args.price_only and args.catalysts is None:
        parser.error("Provide --catalysts symbol,date CSV, or explicitly choose --price-only")
    if not 0 < args.min_gap_pct < StockGapConfig().max_gap_pct:
        parser.error("--min-gap-pct must be positive and below the maximum gap")
    config = replace(StockGapConfig(), min_gap_pct=args.min_gap_pct)

    from backtest.equity_universe import load_equity_universe
    indexes = tuple(args.indexes)
    symbols = load_equity_universe(indexes)
    data = load_database_data(symbols, args.start - timedelta(days=40), args.end,
                              database_only=args.database_only,
                              skip_missing_instruments=True)
    candidates = daily_candidates(data, load_catalysts(args.catalysts),
                                  price_only=args.price_only, config=config)

    from nifty_15m_env import load_nifty_environment
    load_nifty_environment()
    from trading.database import get_db
    kite = catalog = None
    if args.fetch_intraday:
        from trading.user_token import fetch_user_token
        kite, _ = fetch_user_token(logging.getLogger(__name__), user_id=975447485)
        catalog = {row["tradingsymbol"]: row["instrument_token"] for row in kite.instruments("NSE")
                   if row.get("instrument_type") == "EQ"}
    evaluated = []
    with get_db() as db:
        cached_sessions = {}
        if not args.fetch_intraday:
            candidates_by_symbol = defaultdict(list)
            for candidate in candidates:
                candidates_by_symbol[candidate["symbol"]].append(candidate["date"])
            for symbol, days in candidates_by_symbol.items():
                first_day, last_day = min(days), max(days)
                start = datetime.combine(first_day - timedelta(days=45), dt_time.min, IST).astimezone(timezone.utc).replace(tzinfo=None)
                end = datetime.combine(last_day + timedelta(days=1), dt_time.min, IST).astimezone(timezone.utc).replace(tzinfo=None)
                grouped = defaultdict(list)
                for bar in read_15m(db, symbol, start, end):
                    grouped[bar.time.date()].append(bar)
                cached_sessions[symbol] = grouped
        for candidate in candidates:
            day = candidate["date"]
            if args.fetch_intraday:
                start_day = day - timedelta(days=45)
                start = datetime.combine(start_day, dt_time.min, IST).astimezone(timezone.utc).replace(tzinfo=None)
                end = datetime.combine(day + timedelta(days=1), dt_time.min, IST).astimezone(timezone.utc).replace(tzinfo=None)
                bars = read_15m(db, candidate["symbol"], start, end)
                grouped = defaultdict(list)
                for bar in bars:
                    grouped[bar.time.date()].append(bar)
            else:
                grouped = cached_sessions.get(candidate["symbol"], {})
            if args.fetch_intraday and (day not in grouped or len(grouped) < 10):
                token = catalog.get(candidate["symbol"])
                if token:
                    persist_15m(db, candidate["symbol"],
                                kite.historical_data(token, start_day, day, "15minute"))
                    db.commit()
                    bars = read_15m(db, candidate["symbol"], start, end)
                    grouped = defaultdict(list)
                    for bar in bars:
                        grouped[bar.time.date()].append(bar)
                    time.sleep(.35)
            result = replay(candidate, grouped, cost_bps=args.cost_bps, config=config)
            if result:
                evaluated.append({"symbol": candidate["symbol"], "date": str(day),
                                  "verified_catalyst": candidate["verified_catalyst"], **result})
    trades = [row for row in evaluated if row["status"] == "trade"]
    report = {"strategy": "stock_catalyst_gap_continuation",
              "universe": "current_" + "+".join(indexes),
              "mode": "price_only_exploratory" if args.price_only else "catalyst_verified",
              "period": {"start": str(args.start), "end": str(args.end)},
              "cost_bps": args.cost_bps, "minimum_gap_pct": args.min_gap_pct,
              "symbols_requested": len(symbols),
              "daily_candidates": len(candidates), "candidates_with_15m_data": len(evaluated),
              "invalid_15m_rows_skipped": read_15m.invalid_rows,
              "metrics": metrics(trades),
              "limitations": ["current constituents create survivorship bias",
                              "first-bar typical price proxies exchange VWAP",
                              "market and sector alignment are not yet point-in-time validated"]
              + (["price-only mode does not verify a material catalyst"] if args.price_only else []),
              "trade_log": evaluated}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2))
    print(json.dumps({key: value for key, value in report.items() if key != "trade_log"}, indent=2))
    print(f"Trade log saved to {args.output}")


if __name__ == "__main__":
    main()
