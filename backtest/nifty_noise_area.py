"""Walk-forward NIFTY replication of time-of-day noise-area momentum.

Research only.  The source paper trades SPY and uses volume-aware exits.  This
NIFTY index adaptation deliberately omits VWAP because the index OHLC source has
no genuine traded volume.  Signals use completed 30-minute observations and
fill at the next minute open.
"""
import argparse
import csv
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import time
from pathlib import Path

from backtest.fixed_profit import load_sessions, statistics


@dataclass(frozen=True)
class Config:
    lookback: int = 14
    volatility_multiplier: float = 1.0
    cost_points: float = 2.0
    slippage_each_fill: float = 0.5
    first_check: time = time(9, 45)
    last_entry: time = time(14, 45)
    forced_exit: time = time(15, 15)
    max_entries: int | None = None

    def __post_init__(self):
        if self.lookback < 2 or self.volatility_multiplier <= 0:
            raise ValueError("Lookback and volatility multiplier must be positive")
        if self.cost_points < 0 or self.slippage_each_fill < 0:
            raise ValueError("Costs and slippage cannot be negative")
        if self.max_entries is not None and self.max_entries < 1:
            raise ValueError("Maximum entries must be positive or None")


def check_indexes(bars, config):
    """Minute indexes whose closes are the 30-minute decision observations."""
    first = config.first_check.hour * 60 + config.first_check.minute
    forced = config.forced_exit.hour * 60 + config.forced_exit.minute
    return [i for i, bar in enumerate(bars)
            if (end := bar.time.hour * 60 + bar.time.minute + 1) >= first
            and (end - (9 * 60 + 15)) % 30 == 0
            and end < forced]


def run(sessions, config=Config()):
    history = defaultdict(list)
    previous_close = None
    trades = []
    for day, bars in sessions:
        checks = check_indexes(bars, config)
        if previous_close is None:
            previous_close = float(bars[-1].close)
            for i in checks:
                history[bars[i].time.time()].append(abs(float(bars[i].close / bars[0].open) - 1))
            continue
        session_open = float(bars[0].open)
        position = None
        pending = None
        entries = 0
        for i, bar in enumerate(bars):
            clock = bar.time.time()
            if clock >= config.forced_exit and position:
                direction = position["direction"]
                exit_price = float(bar.open) - direction * config.slippage_each_fill
                points = direction * (exit_price - position["entry"]) - config.cost_points
                trades.append({**position, "exit_time": bar.time.isoformat(),
                               "exit": exit_price, "points": points,
                               "exit_reason": "session_end"})
                position = None
                pending = None
            if pending is not None:
                fill = float(bar.open)
                if pending == 0 and position:
                    direction = position["direction"]
                    exit_price = fill - direction * config.slippage_each_fill
                    points = direction * (exit_price - position["entry"]) - config.cost_points
                    trades.append({**position, "exit_time": bar.time.isoformat(),
                                   "exit": exit_price, "points": points,
                                   "exit_reason": "band_or_session"})
                    position = None
                elif pending in (-1, 1):
                    if position:
                        direction = position["direction"]
                        exit_price = fill - direction * config.slippage_each_fill
                        points = direction * (exit_price - position["entry"]) - config.cost_points
                        trades.append({**position, "exit_time": bar.time.isoformat(),
                                       "exit": exit_price, "points": points,
                                       "exit_reason": "signal_flip"})
                    entry = fill + pending * config.slippage_each_fill
                    position = {"date": str(day), "direction": pending,
                                "entry_time": bar.time.isoformat(), "entry": entry}
                    entries += 1
                pending = None
            if clock >= config.forced_exit:
                if position:
                    pending = 0
                continue
            if i not in checks:
                continue
            moves = history[clock]
            if len(moves) < config.lookback:
                continue
            sigma = sum(moves[-config.lookback:]) / config.lookback
            upper = max(session_open, previous_close) * (1 + config.volatility_multiplier * sigma)
            lower = min(session_open, previous_close) * (1 - config.volatility_multiplier * sigma)
            close = float(bar.close)
            desired = 1 if close > upper else -1 if close < lower else 0
            current = position["direction"] if position else 0
            # The current same-side boundary is the trailing stop.  Re-enter
            # only after a later completed check again establishes imbalance.
            if desired != current:
                if (desired and clock <= config.last_entry and
                        (config.max_entries is None or entries < config.max_entries)):
                    pending = desired
                elif position:
                    pending = 0
        for i in checks:
            history[bars[i].time.time()].append(abs(float(bars[i].close / bars[0].open) - 1))
        previous_close = float(bars[-1].close)
    return trades


def split(trades):
    return {
        "development_2020_2023": statistics([t for t in trades if t["date"] < "2024"]),
        "validation_2024_2025": statistics([t for t in trades if "2024" <= t["date"] < "2026"]),
        "holdout_2026": statistics([t for t in trades if t["date"] >= "2026"]),
        "all": statistics(trades),
    }


def adjusted_statistics(trades, extra_cost):
    adjusted = [{**trade, "points": trade["points"] - extra_cost} for trade in trades]
    return statistics(adjusted)


def analyze(source, output):
    sessions, excluded = load_sessions(source)
    # VM=1 is the paper specification. VM=1.5 is disclosed as the paper's
    # retrospective risk-adjusted leader. One-entry variants are predeclared
    # operational risk controls. Select only on our development set.
    candidates = []
    for vm in (1.0, 1.5):
        for max_entries in (None, 1):
            config = Config(volatility_multiplier=vm, max_entries=max_entries)
            trades = run(sessions, config)
            candidates.append({"config": config, "trades": trades, "score": split(trades)})
    leader = max(candidates, key=lambda x: x["score"]["development_2020_2023"]["profit_factor"] or 0)
    periods = leader["score"]
    yearly = {str(year): statistics([t for t in leader["trades"]
                                     if t["date"].startswith(str(year))])
              for year in range(2020, 2027)}
    cost_stress = {str(total): adjusted_statistics(leader["trades"], total - 3)
                   for total in (3, 4, 5, 6, 8)}
    positive_years = sum(row["net_points"] > 0 for row in yearly.values())
    accepted = all(periods[name]["trades"] >= minimum and
                   periods[name]["net_points"] > 0 and
                   (periods[name]["profit_factor"] or 0) >= 1.05
                   for name, minimum in (("development_2020_2023", 100),
                                         ("validation_2024_2025", 50),
                                         ("holdout_2026", 25))) and positive_years >= 6 \
        and cost_stress["6"]["net_points"] > 0
    serial = lambda row: {"volatility_multiplier": row["config"].volatility_multiplier,
                          "max_entries": row["config"].max_entries,
                          "score": row["score"]}
    report = {
        "strategy": "NIFTY noise-area momentum replication",
        "limitations": ["NIFTY index-point proxy, not option premium P&L",
                        "No VWAP: index source has no genuine volume",
                        "Two points cost plus 0.5 point adverse slippage on every fill"],
        "complete_sessions": len(sessions), "excluded_dates": excluded,
        "selection": "VM selected on 2020-2023 only; later periods not used",
        "candidates": [serial(row) for row in candidates],
        "development_leader": serial(leader),
        "leader_yearly": yearly,
        "leader_cost_stress_total_points_per_trade": cost_stress,
        "acceptance_policy": "All chronological periods PF >=1.05 and positive; >=6/7 positive years; positive at six total friction points",
        "accepted": accepted,
        "production_action": "PAPER_ONLY" if accepted else "KEEP_DISABLED",
    }
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    (output / "results.json").write_text(json.dumps(report, indent=2))
    if leader["trades"]:
        with (output / "trades.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=leader["trades"][0].keys())
            writer.writeheader(); writer.writerows(leader["trades"])
    print(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("--output", default="outputs/nifty-noise-area")
    args = parser.parse_args()
    analyze(args.source, args.output)
