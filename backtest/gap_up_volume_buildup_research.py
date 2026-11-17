"""Research conditional opening-volume thresholds after prior daily volume buildup."""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from statistics import mean, median

from backtest.nifty500_gap_up import IST, read_15m, replay
from backtest.smallcap_momentum_swing import load_database_data
from strategies.gap_up import StockGapConfig


def prior_volume_signal(rows, index, lookback, multiple, mode):
    """Use only sessions completed before the gap day."""
    matches = []
    for position in range(max(20, index - lookback), index):
        history = [bar.volume for _, bar in rows[position - 20:position] if bar.volume > 0]
        if len(history) < 15:
            continue
        bar = rows[position][1]
        previous = rows[position - 1][1]
        ratio = bar.volume / median(history)
        close_location = ((bar.close - bar.low) / (bar.high - bar.low)
                          if bar.high > bar.low else .5)
        bullish = bar.close > previous.close
        strong = bullish and bar.close > bar.open and close_location >= .60
        qualifies = ratio >= multiple and (
            mode == "any" or (mode == "bullish" and bullish) or
            (mode == "strong" and strong)
        )
        if qualifies:
            matches.append((ratio, str(rows[position][0])))
    return max(matches, default=None)


def metrics(rows):
    values = [row["net_return_pct"] for row in rows]
    gains = sum(max(value, 0) for value in values)
    losses = -sum(min(value, 0) for value in values)
    return {
        "trades": len(values),
        "win_rate_pct": round(100 * sum(value > 0 for value in values) / len(values), 2) if values else None,
        "average_net_trade_pct": round(mean(values), 4) if values else None,
        "profit_factor": round(gains / losses, 3) if losses else None,
        "compounded_return_pct": round(100 * (math.prod(1 + value / 100 for value in values) - 1), 2) if values else None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    baseline = json.loads(args.baseline.read_text())
    candidate_rows = baseline["trade_log"]
    symbols = tuple(sorted({row["symbol"] for row in candidate_rows}))
    start = date.fromisoformat(baseline["period"]["start"]) - timedelta(days=80)
    end = date.fromisoformat(baseline["period"]["end"])
    daily = load_database_data(symbols, start, end, database_only=True,
                               skip_missing_instruments=True)
    indexes = {symbol: {day: index for index, (day, _) in enumerate(rows)}
               for symbol, rows in daily.items()}

    from nifty_15m_env import load_nifty_environment
    load_nifty_environment()
    from trading.database import get_db
    prepared = []
    with get_db() as db:
        for raw in candidate_rows:
            symbol, day = raw["symbol"], date.fromisoformat(raw["date"])
            rows = daily.get(symbol, [])
            index = indexes.get(symbol, {}).get(day)
            if index is None or index < 24:
                continue
            start_stamp = datetime.combine(day - timedelta(days=45), time.min, IST).astimezone(timezone.utc).replace(tzinfo=None)
            end_stamp = datetime.combine(day + timedelta(days=1), time.min, IST).astimezone(timezone.utc).replace(tzinfo=None)
            bars = read_15m(db, symbol, start_stamp, end_stamp)
            sessions = {}
            for bar in bars:
                sessions.setdefault(bar.time.date(), []).append(bar)
            prepared.append({"symbol": symbol, "date": day, "rows": rows,
                             "index": index, "sessions": sessions})

    configurations = []
    for lookback in (1, 2, 3, 4):
        for multiple in (1.25, 1.5, 2.0, 2.5, 3.0):
            for mode in ("any", "bullish", "strong"):
                for relaxed_rvol in (.75, 1.0, 1.2, 1.5):
                    trades = []
                    for item in prepared:
                        signal = prior_volume_signal(item["rows"], item["index"], lookback,
                                                     multiple, mode)
                        minimum_rvol = relaxed_rvol if signal else 1.5
                        config = replace(StockGapConfig(),
                                         min_opening_relative_volume=minimum_rvol)
                        index = item["index"]
                        candidate = {"date": item["date"],
                                     "previous": item["rows"][index - 1][1]}
                        result = replay(candidate, item["sessions"], cost_bps=20.0,
                                        config=config)
                        if result and result["status"] == "trade":
                            risk_pct = 100 * (result["entry"] / result["stop"] - 1)
                            trades.append({"symbol": item["symbol"],
                                           "date": str(item["date"]),
                                           "prior_volume_signal": bool(signal),
                                           "prior_volume_ratio": signal[0] if signal else None,
                                           "prior_volume_day": signal[1] if signal else None,
                                           "risk_pct": risk_pct, **result})
                    for min_risk, max_risk in ((0, 3.01), (.5, 2.5), (.75, 2.0), (1.0, 2.0)):
                        selected = [row for row in trades
                                    if min_risk <= row["risk_pct"] < max_risk]
                        development = [row for row in selected if row["date"] < "2024-01-01"]
                        validation = [row for row in selected if row["date"] >= "2024-01-01"]
                        configurations.append({
                            "lookback_sessions": lookback,
                            "prior_volume_multiple": multiple,
                            "prior_volume_mode": mode,
                            "buildup_opening_rvol": relaxed_rvol,
                            "minimum_risk_pct": min_risk,
                            "maximum_risk_pct": max_risk,
                            "development": metrics(development),
                            "validation": metrics(validation),
                            "overall": metrics(selected),
                            "trades": selected,
                        })

    eligible = [row for row in configurations
                if row["development"]["trades"] >= 20
                and row["validation"]["trades"] >= 20
                and (row["development"]["profit_factor"] or 0) > 1
                and (row["validation"]["profit_factor"] or 0) > 1
                and row["development"]["average_net_trade_pct"] > 0
                and row["validation"]["average_net_trade_pct"] > 0]
    eligible.sort(key=lambda row: (
        min(row["development"]["profit_factor"], row["validation"]["profit_factor"]),
        row["overall"]["trades"]), reverse=True)
    payload = {
        "prepared_candidates": len(prepared),
        "tested_configurations": len(configurations),
        "selection_rule": "at least 20 trades in both 2020-2023 and 2024-2026; positive average and PF>1 in both",
        "qualifying_configurations": len(eligible),
        "top_configurations": eligible[:20],
        "highest_trade_count_configurations": sorted(
            eligible, key=lambda row: (row["overall"]["trades"],
                                       min(row["development"]["profit_factor"],
                                           row["validation"]["profit_factor"])),
            reverse=True)[:20],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2))
    compact = lambda rows: [{key: value for key, value in row.items() if key != "trades"}
                            for row in rows]
    print(json.dumps({**payload, "top_configurations": compact(eligible[:10]),
                      "highest_trade_count_configurations": compact(
                          payload["highest_trade_count_configurations"][:10])}, indent=2))


if __name__ == "__main__":
    main()
