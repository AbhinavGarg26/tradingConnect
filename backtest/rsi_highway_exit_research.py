"""Second-stage RSI Highway research for exit and stop design."""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import date
from itertools import product
from pathlib import Path

from backtest.equity_universe import load_equity_universe
from backtest.rsi_highway import breadth_by_day, load_daily_candles, run_symbol
from backtest.rsi_highway_research import metrics
from strategies.rsi_highway import Config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    symbols = load_equity_universe()
    data = load_daily_candles(symbols, date(2020, 1, 1), date(2026, 11, 14),
                              database_only=True, skip_missing_instruments=True)
    data = {symbol: rows for symbol, rows in data.items() if rows}
    breadth = breadth_by_day(data)
    results = []
    for exit_rsi, swing_lookback, tight_stop in product((45.0, 50.0, 55.0), (3, 5, 10), (False, True)):
        config = Config(trend_rsi=55, confirmation_rsi=55,
                        require_weekly_rsi_rising=False, exit_rsi=exit_rsi,
                        swing_lookback=swing_lookback, tight_initial_stop=tight_stop)
        trades = []
        for symbol, rows in data.items():
            values, _ = run_symbol(symbol, rows, breadth, breadth_minimum=.05,
                                   cost_bps=10.0, config=config)
            trades.extend(values)
        trades.sort(key=lambda row: (row["exit"], row["symbol"]))
        dev = [row for row in trades if row["entry"] < "2024-01-01"]
        val = [row for row in trades if row["entry"] >= "2024-01-01"]
        results.append({"config": asdict(config), "development": metrics(dev),
                        "validation": metrics(val), "all": metrics(trades)})
    results.sort(key=lambda row: min(row["development"]["average_net_cycle_pct"] or -999,
                                     row["validation"]["average_net_cycle_pct"] or -999), reverse=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"results": results}, indent=2))
    print(json.dumps(results[:10], indent=2))


if __name__ == "__main__":
    main()
