"""Targeted robustness research for opening-bar confirmation thresholds."""
from __future__ import annotations

import argparse
import json
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from backtest.gap_up_volume_buildup_research import metrics, prior_volume_signal
from backtest.nifty500_gap_up import IST, read_15m, replay
from backtest.smallcap_momentum_swing import load_database_data
from strategies.gap_up import StockGapConfig


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    baseline = json.loads(args.baseline.read_text())
    source = baseline["trade_log"]
    symbols = tuple(sorted({row["symbol"] for row in source}))
    start = date.fromisoformat(baseline["period"]["start"]) - timedelta(days=80)
    end = date.fromisoformat(baseline["period"]["end"])
    daily = load_database_data(symbols, start, end, database_only=True,
                               skip_missing_instruments=True)
    indexes = {symbol: {day: i for i, (day, _) in enumerate(rows)}
               for symbol, rows in daily.items()}
    from nifty_15m_env import load_nifty_environment
    load_nifty_environment()
    from trading.database import get_db
    prepared = []
    with get_db() as db:
        for raw in source:
            symbol, day = raw["symbol"], date.fromisoformat(raw["date"])
            rows = daily.get(symbol, [])
            index = indexes.get(symbol, {}).get(day)
            if index is None or index < 24:
                continue
            first = datetime.combine(day - timedelta(days=45), time.min, IST).astimezone(timezone.utc).replace(tzinfo=None)
            finish = datetime.combine(day + timedelta(days=1), time.min, IST).astimezone(timezone.utc).replace(tzinfo=None)
            sessions = {}
            for bar in read_15m(db, symbol, first, finish):
                sessions.setdefault(bar.time.date(), []).append(bar)
            buildup = prior_volume_signal(rows, index, 4, 1.25, "any")
            prepared.append({"symbol": symbol, "day": day, "rows": rows,
                             "index": index, "sessions": sessions,
                             "buildup": buildup})

    configurations = []
    for location in (.60, .65, .70, .75):
        for wick in (.35, .45, .55):
            for retained in (.40, .50, .60):
                for cutoff in (time(11, 30), time(12, 30), time(13, 30)):
                    trades = []
                    for item in prepared:
                        config = replace(
                            StockGapConfig(),
                            min_opening_relative_volume=.75 if item["buildup"] else 1.5,
                            min_opening_close_location=location,
                            max_upper_wick_ratio=wick,
                            min_gap_retained=retained,
                        )
                        result = replay(
                            {"date": item["day"],
                             "previous": item["rows"][item["index"] - 1][1]},
                            item["sessions"], cost_bps=20.0, config=config,
                            entry_cutoff=cutoff,
                        )
                        if not result or result["status"] != "trade":
                            continue
                        risk = 100 * (result["entry"] / result["stop"] - 1)
                        if not .75 <= risk < 2.0:
                            continue
                        trades.append({"symbol": item["symbol"],
                                       "date": str(item["day"]),
                                       "prior_volume_signal": bool(item["buildup"]),
                                       "risk_pct": risk, **result})
                    dev = [row for row in trades if row["date"] < "2024-01-01"]
                    val = [row for row in trades if row["date"] >= "2024-01-01"]
                    configurations.append({
                        "opening_close_location": location,
                        "maximum_upper_wick": wick,
                        "minimum_gap_retained": retained,
                        "entry_cutoff": cutoff.isoformat(),
                        "development": metrics(dev),
                        "validation": metrics(val),
                        "overall": metrics(trades),
                        "trades": trades,
                    })
    eligible = [row for row in configurations
                if row["development"]["trades"] >= 20
                and row["validation"]["trades"] >= 20
                and row["development"]["average_net_trade_pct"] > 0
                and row["validation"]["average_net_trade_pct"] > 0
                and (row["development"]["profit_factor"] or 0) > 1
                and (row["validation"]["profit_factor"] or 0) > 1]
    eligible.sort(key=lambda row: (row["overall"]["trades"],
                                   min(row["development"]["profit_factor"],
                                       row["validation"]["profit_factor"])),
                  reverse=True)
    payload = {"prepared_candidates": len(prepared),
               "tested_configurations": len(configurations),
               "qualifying_configurations": len(eligible),
               "best_by_trade_count": eligible[:20]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2))
    print(json.dumps({**payload, "best_by_trade_count": [
        {key: value for key, value in row.items() if key != "trades"}
        for row in eligible[:10]]}, indent=2))


if __name__ == "__main__":
    main()
