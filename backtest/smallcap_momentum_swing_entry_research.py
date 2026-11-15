"""Research entry-quality filters for Smallcap Momentum Swing baseline trades.

This is a conservative candidate-level screen: it only removes trades from the
baseline and never invents trades that the original position state skipped.
The selected filter must subsequently be replayed by the full backtester.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import date
from itertools import product
from pathlib import Path
from statistics import mean
from statistics import median

from backtest.equity_universe import load_equity_universe
from backtest.smallcap_momentum_swing import load_database_data
from strategies.smallcap_momentum_swing import Action, Config, scan_for_focus


SPLITS = {
    "development_2020_2022": (date(2020, 1, 1), date(2022, 12, 31)),
    "validation_2023_2024": (date(2023, 1, 1), date(2024, 12, 31)),
    "test_2025_2026": (date(2025, 1, 1), date(2026, 12, 31)),
}


def metrics(rows):
    values = [row["net_return_pct"] for row in rows]
    wins = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value <= 0)
    return {
        "trades": len(values),
        "win_rate_pct": round(100 * sum(value > 0 for value in values) / len(values), 2) if values else None,
        "average_net_trade_pct": round(mean(values), 4) if values else None,
        "sum_net_trade_pct": round(sum(values), 3),
        "profit_factor": round(wins / losses, 4) if losses else None,
        "entry_session_stop_pct": round(100 * sum(
            row["exit_reason"] == "entry_session_stop_daily_bar_proxy" for row in rows
        ) / len(rows), 2) if rows else None,
    }


def build_market_context(data):
    """Point-in-time breadth and median 60-session return by session."""
    values = defaultdict(lambda: {"above50": 0, "eligible50": 0,
                                  "above200": 0, "eligible200": 0, "returns60": []})
    for rows in data.values():
        sum50 = sum200 = 0.0
        closes = []
        for index, (day, bar) in enumerate(rows):
            closes.append(bar.close)
            sum50 += bar.close
            sum200 += bar.close
            if index >= 50:
                sum50 -= closes[index - 50]
            if index >= 200:
                sum200 -= closes[index - 200]
            if index >= 49:
                values[day]["eligible50"] += 1
                values[day]["above50"] += bar.close > sum50 / 50
            if index >= 199:
                values[day]["eligible200"] += 1
                values[day]["above200"] += bar.close > sum200 / 200
            if index >= 60:
                values[day]["returns60"].append(100 * (bar.close / closes[index - 60] - 1))
    result = {day: {
        "breadth50_pct": 100 * value["above50"] / value["eligible50"] if value["eligible50"] else 0,
        "breadth200_pct": 100 * value["above200"] / value["eligible200"] if value["eligible200"] else 0,
        "median_return60_pct": median(value["returns60"]) if value["returns60"] else 0,
    } for day, value in values.items()}
    days = sorted(result)
    for index, day in enumerate(days):
        prior = result[days[index - 20]]["breadth50_pct"] if index >= 20 else result[day]["breadth50_pct"]
        result[day]["breadth50_change20"] = result[day]["breadth50_pct"] - prior
    return result


def enrich_trades(trades, data, market_context, config=Config()):
    by_symbol = defaultdict(list)
    for trade in trades:
        by_symbol[trade["symbol"]].append(trade)
    enriched = []
    for symbol, symbol_trades in by_symbol.items():
        rows = data.get(symbol, [])
        indexes = {str(day): index for index, (day, _) in enumerate(rows)}
        latest_focus = None
        focus_at_index = {}
        for index, (_, bar) in enumerate(rows):
            if index >= config.scan_lookback:
                start = max(0, index - config.scan_lookback)
                scan = scan_for_focus([item for _, item in rows[start:index + 1]], config)
                if scan.action == Action.SCAN:
                    latest_focus = index
            focus_at_index[index] = latest_focus
        for trade in symbol_trades:
            entry_index = indexes.get(trade["entry"])
            if entry_index is None or entry_index < 61:
                continue
            setup_index = entry_index - 1
            setup_day, setup = rows[setup_index]
            _, entry = rows[entry_index]
            window = [item for _, item in rows[setup_index - 59:setup_index + 1]]
            impulse_high = max(item.high for item in window)
            sma20 = mean(item.close for _, item in rows[setup_index - 19:setup_index + 1])
            sma50 = mean(item.close for _, item in rows[setup_index - 49:setup_index + 1])
            stock_return60 = 100 * (setup.close / rows[setup_index - 60][1].close - 1)
            average_turnover_crore = mean(
                item.close * item.volume for _, item in rows[setup_index - 19:setup_index + 1]
            ) / 10_000_000
            context = market_context.get(setup_day, {})
            lows3 = [item.low for _, item in rows[setup_index - 2:setup_index + 1]]
            latest_focus = focus_at_index.get(setup_index)
            row = dict(trade)
            row["features"] = {
                "gap_pct": 100 * (entry.open / setup.high - 1),
                "initial_risk_pct": 100 * (entry.open / setup.low - 1),
                "pullback_pct": 100 * (1 - setup.close / impulse_high),
                "entry_extension_sma20_pct": 100 * (entry.open / sma20 - 1),
                "three_low_rise_pct": 100 * (lows3[-1] / lows3[0] - 1),
                "focus_age_sessions": setup_index - latest_focus if latest_focus is not None else 999,
                "breadth50_pct": context.get("breadth50_pct", 0),
                "breadth200_pct": context.get("breadth200_pct", 0),
                "breadth50_change20": context.get("breadth50_change20", 0),
                "market_return60_pct": context.get("median_return60_pct", 0),
                "relative_strength60_pct": stock_return60 - context.get("median_return60_pct", 0),
                "setup_extension_sma50_pct": 100 * (setup.close / sma50 - 1),
                "average_turnover_crore": average_turnover_crore,
            }
            enriched.append(row)
    return enriched


def accepts(row, rules):
    value = row["features"]
    return (value["gap_pct"] <= rules["max_gap_pct"]
            and value["initial_risk_pct"] <= rules["max_initial_risk_pct"]
            and value["pullback_pct"] >= rules["min_pullback_pct"]
            and value["entry_extension_sma20_pct"] <= rules["max_entry_extension_sma20_pct"]
            and value["three_low_rise_pct"] <= rules["max_three_low_rise_pct"]
            and value["focus_age_sessions"] <= rules["max_focus_age_sessions"])


def bucket_metrics(rows, feature, boundaries):
    result = []
    lower = float("-inf")
    for upper in [*boundaries, float("inf")]:
        selected = [row for row in rows if lower < row["features"][feature] <= upper]
        label = f"({lower},{upper}]"
        result.append({"bucket": label, **metrics(selected)})
        lower = upper
    return result


def range_screen(rows, **ranges):
    return [row for row in rows if all(
        lower <= row["features"][feature] <= upper
        for feature, (lower, upper) in ranges.items()
    )]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    baseline = json.loads(args.baseline.read_text())
    symbols = load_equity_universe(("midcap150", "smallcap500"))
    data = load_database_data(symbols, date(2020, 1, 1), date(2026, 11, 14),
                              database_only=True, skip_missing_instruments=True)
    market_context = build_market_context(data)
    trades = enrich_trades(baseline["trade_log"], data, market_context)
    grids = product(
        (1.0, 2.0, 3.0, 5.0),       # maximum gap above setup high
        (3.0, 4.0, 5.0, 6.0, 8.0),  # maximum entry-to-stop risk
        (0.0, 3.0, 5.0, 8.0),       # minimum real pullback
        (5.0, 8.0, 10.0, 15.0),     # maximum extension above SMA20
        (1.0, 2.0, 3.0, 5.0),       # cap climb across three higher lows
        (20, 40, 60, 90),            # recency of volume breakout
    )
    results = []
    for gap, risk, pullback, extension, low_rise, focus_age in grids:
        rules = {
            "max_gap_pct": gap, "max_initial_risk_pct": risk,
            "min_pullback_pct": pullback,
            "max_entry_extension_sma20_pct": extension,
            "max_three_low_rise_pct": low_rise,
            "max_focus_age_sessions": focus_age,
        }
        selected = [row for row in trades if accepts(row, rules)]
        split_metrics = {}
        for name, (start, end) in SPLITS.items():
            split_metrics[name] = metrics([
                row for row in selected if start <= date.fromisoformat(row["entry"]) <= end
            ])
        minimum_trades = min(value["trades"] for value in split_metrics.values())
        averages = [value["average_net_trade_pct"] for value in split_metrics.values()]
        factors = [value["profit_factor"] for value in split_metrics.values()]
        if minimum_trades >= 75 and all(value is not None for value in averages + factors):
            robust_score = min(averages) + .1 * min(factors)
            results.append({"rules": rules, "all": metrics(selected), "splits": split_metrics,
                            "robust_score": round(robust_score, 6)})
    results.sort(key=lambda row: row["robust_score"], reverse=True)
    targeted_ranges = {
        "balanced_entry_zone": {
            "gap_pct": (-999, 2), "initial_risk_pct": (-999, 3),
            "pullback_pct": (5, 8), "entry_extension_sma20_pct": (0, 8),
            "three_low_rise_pct": (0, 3), "focus_age_sessions": (20, 60),
        },
        "precise_positive_buckets": {
            "gap_pct": (-999, 2), "initial_risk_pct": (-999, 3),
            "pullback_pct": (5, 8), "entry_extension_sma20_pct": (5, 8),
            "three_low_rise_pct": (2, 3), "focus_age_sessions": (40, 60),
        },
        "relaxed_entry_zone": {
            "gap_pct": (-999, 2), "initial_risk_pct": (-999, 4),
            "pullback_pct": (3, 8), "entry_extension_sma20_pct": (0, 10),
            "three_low_rise_pct": (0, 5), "focus_age_sessions": (20, 60),
        },
        "anti_extension_market_guard": {
            "gap_pct": (-999, 2), "initial_risk_pct": (-999, 4),
            "pullback_pct": (3, 12), "entry_extension_sma20_pct": (-5, 10),
            "three_low_rise_pct": (0, 5), "focus_age_sessions": (20, 60),
            "breadth50_pct": (20, 90), "relative_strength60_pct": (-20, 20),
            "setup_extension_sma50_pct": (-10, 20),
            "average_turnover_crore": (10, 100),
        },
        "broad_market_guard": {
            "gap_pct": (-999, 2), "initial_risk_pct": (-999, 4),
            "pullback_pct": (3, 15), "entry_extension_sma20_pct": (-10, 10),
            "three_low_rise_pct": (0, 5), "focus_age_sessions": (20, 90),
            "breadth50_pct": (20, 90), "relative_strength60_pct": (-20, 30),
            "setup_extension_sma50_pct": (-10, 20),
            "average_turnover_crore": (10, 999999),
        },
        "improving_market_guard": {
            "gap_pct": (-999, 2), "initial_risk_pct": (-999, 4),
            "pullback_pct": (3, 15), "entry_extension_sma20_pct": (-10, 10),
            "three_low_rise_pct": (0, 5), "focus_age_sessions": (20, 90),
            "breadth50_pct": (20, 90), "breadth50_change20": (0, 999),
            "market_return60_pct": (0, 999),
            "relative_strength60_pct": (-20, 30),
            "setup_extension_sma50_pct": (-10, 20),
            "average_turnover_crore": (10, 999999),
        },
    }
    targeted = {}
    for name, ranges in targeted_ranges.items():
        selected = range_screen(trades, **ranges)
        targeted[name] = {
            "ranges": ranges, "all": metrics(selected),
            "splits": {split: metrics([row for row in selected
                                       if start <= date.fromisoformat(row["entry"]) <= end])
                       for split, (start, end) in SPLITS.items()},
            "by_entry_year": {
                year: metrics([row for row in selected if row["entry"].startswith(year)])
                for year in sorted({row["entry"][:4] for row in selected})
            },
        }
    payload = {
        "baseline": metrics(trades), "enriched_trades": len(trades),
        "split_definition": {key: [str(value[0]), str(value[1])] for key, value in SPLITS.items()},
        "candidate_level_caveat": "Filters baseline trades only; final rules require full stateful replay.",
        "diagnostics": {
            "gap_pct": bucket_metrics(trades, "gap_pct", (1, 2, 3, 5)),
            "initial_risk_pct": bucket_metrics(trades, "initial_risk_pct", (3, 4, 5, 6, 8, 10)),
            "pullback_pct": bucket_metrics(trades, "pullback_pct", (0, 3, 5, 8, 12, 20)),
            "entry_extension_sma20_pct": bucket_metrics(
                trades, "entry_extension_sma20_pct", (0, 5, 8, 10, 15, 20)
            ),
            "three_low_rise_pct": bucket_metrics(trades, "three_low_rise_pct", (1, 2, 3, 5, 8)),
            "focus_age_sessions": bucket_metrics(trades, "focus_age_sessions", (20, 40, 60, 90, 120)),
            "breadth50_pct": bucket_metrics(trades, "breadth50_pct", (20, 30, 40, 50, 60, 70, 80)),
            "breadth200_pct": bucket_metrics(trades, "breadth200_pct", (20, 30, 40, 50, 60, 70, 80)),
            "breadth50_change20": bucket_metrics(
                trades, "breadth50_change20", (-30, -20, -10, 0, 10, 20, 30)
            ),
            "market_return60_pct": bucket_metrics(
                trades, "market_return60_pct", (-20, -10, -5, 0, 5, 10, 20, 30)
            ),
            "relative_strength60_pct": bucket_metrics(
                trades, "relative_strength60_pct", (-20, -10, 0, 10, 20, 40, 60)
            ),
            "setup_extension_sma50_pct": bucket_metrics(
                trades, "setup_extension_sma50_pct", (-10, 0, 5, 10, 20, 30, 50)
            ),
            "average_turnover_crore": bucket_metrics(
                trades, "average_turnover_crore", (1, 2, 5, 10, 25, 50, 100)
            ),
            "by_entry_year": {year: metrics([row for row in trades if row["entry"].startswith(year)])
                              for year in sorted({row["entry"][:4] for row in trades})},
        },
        "targeted_entry_zones": targeted,
        "variants_evaluated": len(results), "top_variants": results[:25],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2))
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
