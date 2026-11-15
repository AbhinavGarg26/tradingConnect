"""Robustness grid for RSI Highway using database-cached broad-universe candles."""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import date
from itertools import product
from pathlib import Path

from backtest.equity_universe import load_equity_universe
from backtest.rsi_highway import breadth_by_day, load_daily_candles, run_symbol
from strategies.rsi_highway import Config


def metrics(rows):
    values = [row["net_return"] for row in rows]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value <= 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    cumulative = peak = drawdown = 0.0
    for value in values:
        cumulative += value
        peak = max(peak, cumulative)
        drawdown = min(drawdown, cumulative - peak)
    return {
        "trades": len(values),
        "win_rate_pct": round(100 * len(wins) / len(values), 2) if values else None,
        "average_net_cycle_pct": round(100 * sum(values) / len(values), 4) if values else None,
        "sum_net_cycle_pct": round(100 * sum(values), 4),
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss else None,
        "max_sequential_drawdown_pct": round(100 * drawdown, 4),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2020, 1, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 11, 14))
    parser.add_argument("--split", type=date.fromisoformat, default=date(2024, 1, 1))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    symbols = load_equity_universe()
    data = load_daily_candles(symbols, args.start, args.end, database_only=True,
                              skip_missing_instruments=True)
    data = {symbol: rows for symbol, rows in data.items() if rows}
    breadth = breadth_by_day(data)
    results = []
    combinations = product(
        (0.00, 0.02, 0.05, 0.10, 0.20),
        (50.0, 55.0, 60.0),
        (50.0, 52.0, 55.0),
        (False, True),
    )
    for breadth_minimum, trend_rsi, confirmation_rsi, weekly_rising in combinations:
        config = Config(trend_rsi=trend_rsi, confirmation_rsi=confirmation_rsi,
                        require_weekly_rsi_rising=weekly_rising)
        trades = []
        for symbol, rows in data.items():
            values, _ = run_symbol(symbol, rows, breadth, breadth_minimum=breadth_minimum,
                                   cost_bps=10.0, config=config)
            trades.extend(values)
        trades.sort(key=lambda row: (row["exit"], row["symbol"]))
        development = [row for row in trades if date.fromisoformat(row["entry"]) < args.split]
        validation = [row for row in trades if date.fromisoformat(row["entry"]) >= args.split]
        results.append({
            "breadth_minimum": breadth_minimum,
            "config": asdict(config),
            "development": metrics(development),
            "validation": metrics(validation),
            "all": metrics(trades),
        })

    def robust_score(row):
        dev, val = row["development"], row["validation"]
        dev_avg = dev["average_net_cycle_pct"] or -999
        val_avg = val["average_net_cycle_pct"] or -999
        sample_penalty = min(dev["trades"], 30) / 30 * min(val["trades"], 30) / 30
        return min(dev_avg, val_avg) * sample_penalty

    results.sort(key=robust_score, reverse=True)
    payload = {
        "universe_symbols_with_data": len(data),
        "period": [str(args.start), str(args.end)],
        "development_before": str(args.split),
        "variants": len(results),
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2))
    print(json.dumps({**{key: value for key, value in payload.items() if key != "results"},
                      "top": results[:10]}, indent=2))


if __name__ == "__main__":
    main()
