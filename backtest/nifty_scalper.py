"""Cost-sensitive index-point backtest for the paper NIFTY scalper."""
import argparse
import csv
import json
from collections import Counter
from datetime import time, timedelta
from pathlib import Path

from backtest.fixed_profit import aggregate, load_sessions, statistics
from strategies.nifty_scalper import NiftyScalper


def run_session(minute_bars, config=None):
    engine = NiftyScalper(**(config or {}))
    trades = []
    active = None
    for i, minute in enumerate(minute_bars):
        if engine.pending and engine.pending.action.startswith("BUY_"):
            engine.confirm_entry(index_price=minute.open)
            active = {
                "date": str(minute.time.date()), "side": engine.position,
                "entry_time": minute.time.isoformat(), "entry": float(engine.entry),
                "stop": float(engine.stop), "target": float(engine.target),
            }
        if engine.position:
            direction = 1 if engine.position == "CE" else -1
            if minute.time.time() >= time(15, 15):
                exit_price = minute.open
            else:
                stopped = minute.low <= engine.stop if direction == 1 else minute.high >= engine.stop
                targeted = minute.high >= engine.target if direction == 1 else minute.low <= engine.target
                # Tick order is unavailable inside a minute. Treat a bar that
                # touches both levels as a stop, the conservative assumption.
                exit_price = engine.stop if stopped else engine.target if targeted else None
            signal = engine.on_tick(minute.time, exit_price) if exit_price is not None else None
            if signal and signal.action.startswith("EXIT_"):
                reason = signal.reason
                points = engine.confirm_exit(index_price=exit_price, timestamp=minute.time)
                trades.append({**active, "exit_time": minute.time.isoformat(),
                               "exit": float(exit_price), "exit_reason": reason,
                               "points": float(points)})
                active = None
            elif engine.position:
                favorable = minute.high if direction == 1 else minute.low
                engine.on_tick(minute.time, favorable)  # update breakeven for later minutes only
        # Clock-aligned 5m candle ending on this one-minute close. The signal is
        # filled no earlier than the next minute's open.
        if i >= 4 and (i + 1) % 5 == 0:
            candle = aggregate(minute_bars[i-4:i+1])
            engine.on_bar(candle, observed_at=minute.time + timedelta(minutes=1))
    if engine.position:
        raise ValueError("Session ended with an unclosed scalper position")
    return trades


def analyze(source, output):
    sessions, excluded = load_sessions(source)
    trades = [trade for _, bars in sessions for trade in run_session(bars)]
    report = {
        "strategy": "nifty_scalper_5m_v1_paper",
        "data_basis": "NIFTY one-minute OHLC aggregated to completed five-minute bars",
        "execution": "signal close; next one-minute open fill; minute OHLC stop/target checks",
        "same_minute_limit": "minute OHLC cannot reconstruct tick order; stop wins if stop and target touch together",
        "complete_sessions": len(sessions), "excluded_dates": excluded,
        "first_date": str(sessions[0][0]) if sessions else None,
        "last_date": str(sessions[-1][0]) if sessions else None,
        "all": {"gross": statistics(trades),
                "cost_1_point": statistics(trades, 1),
                "cost_2_points": statistics(trades, 2),
                "cost_3_points": statistics(trades, 3)},
        "by_side_cost_2": {side: statistics([t for t in trades if t["side"] == side], 2)
                            for side in ("CE", "PE")},
        "yearly_cost_2": {
            str(year): statistics([t for t in trades if t["date"].startswith(str(year))], 2)
            for year in range(2020, 2027)
        },
        "regimes_cost_2": {
            "development_2020_2023": statistics([t for t in trades if "2020" <= t["date"][:4] <= "2023"], 2),
            "validation_2024_2025": statistics([t for t in trades if "2024" <= t["date"][:4] <= "2025"], 2),
            "holdout_2026": statistics([t for t in trades if t["date"].startswith("2026")], 2),
        },
        "exit_reasons": dict(Counter(trade["exit_reason"] for trade in trades)),
    }
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    (output / "results.json").write_text(json.dumps(report, indent=2))
    if trades:
        with (output / "trades.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(trades[0]))
            writer.writeheader(); writer.writerows(trades)
    print(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="Complete NIFTY one-minute CSV")
    parser.add_argument("--output", default="outputs/nifty-scalper-v1")
    args = parser.parse_args()
    analyze(args.source, args.output)
