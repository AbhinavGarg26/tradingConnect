"""Walk-forward research for NIFTY scalper v2; selection uses 2020-2023 only.

The best development candidate is reported as a leader, not silently promoted.
Promotion requires positive cost-adjusted results in development, validation,
and holdout periods, plus minimum sample-size and drawdown controls.
"""
import argparse
import json
from datetime import time
from itertools import product
from pathlib import Path

from backtest.fixed_profit import load_sessions, statistics
from backtest.nifty_scalper import run_session

MIN_DEVELOPMENT_TRADES = 250
MIN_VALIDATION_TRADES = 100
MIN_HOLDOUT_TRADES = 50
MIN_PROFIT_FACTOR = 1.05
MAX_DRAWDOWN_POINTS = 500


def period(trades, start, end):
    return [trade for trade in trades if start <= trade["date"] < end]


def evaluate(sessions, config):
    trades = [trade for _, bars in sessions for trade in run_session(bars, config)]
    return trades, {
        "development": statistics(period(trades, "2020", "2024"), 2),
        "validation": statistics(period(trades, "2024", "2026"), 2),
        "holdout": statistics(period(trades, "2026", "2027"), 2),
        "all_cost_1": statistics(trades, 1),
        "all_cost_2": statistics(trades, 2),
    }


def promotion_check(score):
    """Return an auditable, deliberately strict paper-promotion decision."""
    requirements = {
        "development_sample": score["development"]["trades"] >= MIN_DEVELOPMENT_TRADES,
        "validation_sample": score["validation"]["trades"] >= MIN_VALIDATION_TRADES,
        "holdout_sample": score["holdout"]["trades"] >= MIN_HOLDOUT_TRADES,
        "development_positive": score["development"]["net_points"] > 0,
        "validation_positive": score["validation"]["net_points"] > 0,
        "holdout_positive": score["holdout"]["net_points"] > 0,
        "development_pf": (score["development"]["profit_factor"] or 0) >= MIN_PROFIT_FACTOR,
        "validation_pf": (score["validation"]["profit_factor"] or 0) >= MIN_PROFIT_FACTOR,
        "holdout_pf": (score["holdout"]["profit_factor"] or 0) >= MIN_PROFIT_FACTOR,
        "all_period_drawdown": score["all_cost_2"]["max_drawdown_points"] <= MAX_DRAWDOWN_POINTS,
    }
    return {"accepted": all(requirements.values()), "requirements": requirements}


def analyze(source, output):
    sessions, excluded = load_sessions(source)
    quality_profiles = [
        dict(min_body_fraction=0.35, min_close_location=0.65, max_extension_atr=0.6),
        dict(min_body_fraction=0.50, min_close_location=0.70, max_extension_atr=0.5),
        dict(min_body_fraction=0.60, min_close_location=0.80, max_extension_atr=0.4),
    ]
    stage_one = []
    for sides, separation, quality, slope in product(
            (("CE", "PE"), ("PE",)), (0.10, 0.20, 0.30, 0.40), quality_profiles, (False, True)):
        config = dict(allowed_sides=sides, min_ema_atr=separation,
                      require_slow_slope=slope, max_entries=1,
                      entry_cutoff=time(12, 0), **quality)
        trades, score = evaluate(sessions, config)
        stage_one.append({"config": config, "score": score, "trades": trades})
    eligible = [row for row in stage_one
                if row["score"]["development"]["trades"] >= MIN_DEVELOPMENT_TRADES]
    leaders = sorted(eligible, key=lambda row: (
        row["score"]["development"]["profit_factor"] or 0,
        row["score"]["development"]["net_points"]), reverse=True)[:5]

    finalists = []
    for leader in leaders:
        base = {k: v for k, v in leader["config"].items()
                if k not in {"max_entries", "entry_cutoff", "reward_multiple"}}
        for max_entries, cutoff, reward in product((1, 2), (time(11, 30), time(12, 0), time(12, 30)), (1.5, 2.0, 2.5)):
            config = dict(base, max_entries=max_entries, entry_cutoff=cutoff,
                          reward_multiple=reward)
            trades, score = evaluate(sessions, config)
            finalists.append({"config": config, "score": score, "trades": trades})
    finalists.sort(key=lambda row: (
        row["score"]["development"]["profit_factor"] or 0,
        row["score"]["development"]["net_points"]), reverse=True)
    leader = next((row for row in finalists
                   if row["score"]["development"]["trades"] >= MIN_DEVELOPMENT_TRADES), None)

    def serializable(row):
        return {"config": {k: str(v) if isinstance(v, time) else list(v) if isinstance(v, tuple) else v
                           for k, v in row["config"].items()}, "score": row["score"]}
    decision = promotion_check(leader["score"]) if leader else {
        "accepted": False, "requirements": {"development_sample": False}}
    report = {
        "selection_policy": "Filters and final configuration ranked only on 2020-2023 PF at 2 points cost; 2024-2025 validation and 2026 holdout not used for selection",
        "complete_sessions": len(sessions), "excluded_dates": excluded,
        "stage_one_candidates": len(stage_one), "final_candidates": len(finalists),
        "acceptance_policy": {
            "cost_points_per_round_trip": 2,
            "minimum_trades": {"development": MIN_DEVELOPMENT_TRADES,
                               "validation": MIN_VALIDATION_TRADES,
                               "holdout": MIN_HOLDOUT_TRADES},
            "minimum_profit_factor_each_period": MIN_PROFIT_FACTOR,
            "maximum_all_period_drawdown_points": MAX_DRAWDOWN_POINTS,
        },
        "development_leader": serializable(leader) if leader else None,
        "promotion_decision": decision,
        "production_action": "KEEP_DISABLED" if not decision["accepted"] else "PAPER_ONLY",
        "top_ten_development_ranked": [serializable(row) for row in finalists[:10]],
    }
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    (output / "research.json").write_text(json.dumps(report, indent=2))
    if leader:
        import csv
        with (output / "development_leader_trades.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(leader["trades"][0]))
            writer.writeheader(); writer.writerows(leader["trades"])
    print(json.dumps({k: v for k, v in report.items() if k != "top_ten_development_ranked"}, indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("--output", default="outputs/nifty-scalper-v2-research")
    args = parser.parse_args()
    analyze(args.source, args.output)
