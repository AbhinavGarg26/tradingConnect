"""Yearly point backtest for the small-profit price-action strategy.

The input is NIFTY one-minute OHLC, not option premiums. Results are therefore
index points and cannot validate the strategy's rupee P&L locks.
"""
import argparse
import csv
import json
from datetime import time, timedelta
from pathlib import Path

from backtest.fixed_profit import aggregate, load_sessions, statistics
from strategies.nifty_opening_range import OpeningSignal
from strategies.small_profit_price_action import SmallProfitPriceAction


def run_session(bars):
    engine = SmallProfitPriceAction(daily_profit_target=10**9,
                                    max_daily_loss=10**9)
    reference = aggregate(bars[:30])
    engine.set_reference(reference, observed_at=bars[30].time)
    if not engine.eligible:
        return []
    trades = []
    entry = None
    details = None
    duration = engine.confirmation_minutes
    for i in range(30, len(bars)):
        minute = bars[i]
        if engine.pending and engine.pending.action.startswith("BUY_"):
            try:
                engine.confirm_entry(index_price=minute.open)
            except ValueError:
                engine.reject_signal()
            else:
                entry = minute.open
                details = {
                    "date": str(minute.time.date()), "side": engine.position,
                    "entry_time": minute.time.isoformat(), "entry": float(entry),
                    "stop": float(engine.stop), "target": float(engine.target),
                }
        if engine.position:
            direction = 1 if engine.position == "CE" else -1
            if minute.time.time() >= time(15, 15):
                exit_price, reason = minute.open, "session_end"
            elif (minute.low <= engine.stop if direction == 1 else minute.high >= engine.stop):
                exit_price = min(minute.open, engine.stop) if direction == 1 else max(minute.open, engine.stop)
                reason = "stop"
            elif (minute.high >= engine.target if direction == 1 else minute.low <= engine.target):
                exit_price, reason = engine.target, "target"
            else:
                continue
            points = direction * (exit_price - entry)
            trades.append({**details, "exit_time": minute.time.isoformat(),
                           "exit": float(exit_price), "exit_reason": reason,
                           "points": float(points)})
            engine.pending = OpeningSignal(f"EXIT_{engine.position}", minute.time,
                                           minute.open, reason)
            engine.confirm_exit()
            engine.record_closed_trade(points)
            entry = details = None
        if (i - 29) % duration == 0:
            engine.on_bar(aggregate(bars[i-duration+1:i+1]),
                          observed_at=minute.time + timedelta(minutes=1))
    if engine.position:
        raise ValueError("Unclosed session position")
    return trades


def analyze(source, output):
    sessions, excluded = load_sessions(source)
    trades = [trade for _, bars in sessions for trade in run_session(bars)]
    yearly = {}
    years = range(sessions[0][0].year, sessions[-1][0].year + 1) if sessions else ()
    for year_number in years:
        year = str(year_number)
        sample = [trade for trade in trades if trade["date"].startswith(year)]
        yearly[year] = {
            "gross": statistics(sample),
            "cost_1_point": statistics(sample, 1),
            "cost_2_points": statistics(sample, 2),
            "CE": statistics([trade for trade in sample if trade["side"] == "CE"]),
            "PE": statistics([trade for trade in sample if trade["side"] == "PE"]),
        }
    report = {
        "strategy": "small_profit_price_action_v1",
        "data_basis": "NIFTY one-minute OHLC; option premium P&L unavailable",
        "assumptions": {
            "entry": "next one-minute open after completed five-minute signal",
            "same_bar_conflict": "stop evaluated before target",
            "forced_exit": "15:15 open",
            "stop_points": 8, "target_points": 12,
        },
        "first_date": str(sessions[0][0]) if sessions else None,
        "last_date": str(sessions[-1][0]) if sessions else None,
        "complete_sessions": len(sessions), "excluded_dates": excluded,
        "all": {"gross": statistics(trades),
                "cost_1_point": statistics(trades, 1),
                "cost_2_points": statistics(trades, 2)},
        "yearly": yearly,
    }
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    (output / "yearly_results.json").write_text(json.dumps(report, indent=2))
    if trades:
        with (output / "trades.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(trades[0]))
            writer.writeheader(); writer.writerows(trades)
    print(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("--output", default="outputs/small-profit-price-action")
    args = parser.parse_args()
    analyze(args.source, args.output)
