"""Stateful research for a close-confirmed Smallcap Momentum entry.

Unlike the v1 daily gap proxy, this waits for a completed breakout candle and
enters on the next session only when the opening price remains near the
breakout close.  It also tests a pullback-pivot base instead of assuming that
any three consecutive higher lows form a good base.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import date
from itertools import product
from pathlib import Path
from statistics import mean, median

from backtest.equity_universe import load_equity_universe
from backtest.smallcap_momentum_swing import load_database_data, sma
from strategies.smallcap_momentum_swing import Action, Config, scan_for_focus


SPLITS = {
    "development_2020_2022": (date(2020, 1, 1), date(2022, 12, 31)),
    "validation_2023_2024": (date(2023, 1, 1), date(2024, 12, 31)),
    "test_2025_2026": (date(2025, 1, 1), date(2026, 12, 31)),
}


def setup_features(rows, index, focus_index, config=Config()):
    if index < config.impulse_lookback - 1 or focus_index is None:
        return None
    window = [bar for _, bar in rows[index - config.impulse_lookback + 1:index + 1]]
    impulse_low = min(row.low for row in window)
    impulse_high = max(row.high for row in window)
    impulse_pct = 100 * (impulse_high / impulse_low - 1)
    pullback_pct = 100 * (1 - window[-1].close / impulse_high)
    if impulse_pct < config.minimum_impulse_pct or not 0 <= pullback_pct <= config.maximum_pullback_pct:
        return None
    current = window[-1]
    current_range = 100 * (current.high - current.low) / current.close
    recent15 = [bar for _, bar in rows[index - 14:index + 1]]
    prior_ranges = [100 * (row.high - row.low) / row.close for row in recent15[:10]]
    if current_range > config.narrow_range_pct or current_range > median(prior_ranges) * config.narrow_vs_median:
        return None
    if current.close < current.low + .5 * (current.high - current.low):
        return None
    recent10 = recent15[-10:]
    pivot_offset = min(range(10), key=lambda offset: recent10[offset].low)
    pivot_age = 9 - pivot_offset
    pivot_low = recent10[pivot_offset].low
    original_higher_lows = recent15[-1].low >= recent15[-2].low >= recent15[-3].low
    pullback_pivot = (2 <= pivot_age <= 8 and current.low > pivot_low
                      and current.close > recent15[-2].close)
    sma20 = mean(row.close for row in recent15[-15:] + [
        bar for _, bar in rows[index - 19:index - 14]
    ])
    # Reversal near SMA20 after a real pullback. This deliberately replaces
    # the three-higher-lows trigger rather than adding another breakout rule.
    sma20_distance = 100 * (current.close / sma20 - 1)
    sma20_pullback_reversal = (
        5 <= pullback_pct <= 20 and -2 <= sma20_distance <= 3
        and current.close > recent15[-2].high
        and current.close >= current.low + .6 * (current.high - current.low)
    )
    return {
        "impulse_base": impulse_low, "pullback_pct": pullback_pct,
        "focus_age": index - focus_index, "original_higher_lows": original_higher_lows,
        "pullback_pivot": pullback_pivot, "setup_high": current.high,
        "sma20_pullback_reversal": sma20_pullback_reversal,
        "setup_low": current.low, "setup_close": current.close,
        "reversal_stop": min(row.low for row in recent15[-5:]),
    }


def candidate_trades(symbol, rows, *, base_rule, max_focus_age, min_pullback,
                     breakout_close_location, breakout_volume_ratio,
                     max_breakout_gap, max_entry_gap, max_initial_risk,
                     max_entry_sma20_extension, entry_mode="close_confirmed",
                     cost_bps=20.0):
    config = Config()
    focus_index = None
    setup = None
    pending_entry = None
    position = None
    trades = []
    for index, (day, bar) in enumerate(rows):
        if index >= config.scan_lookback and scan_for_focus(
                [item for _, item in rows[index - config.scan_lookback:index + 1]], config
        ).action == Action.SCAN:
            focus_index = index

        if position:
            stop = position["stop"]
            if bar.open <= stop or bar.low <= stop:
                exit_price = min(bar.open, stop)
                gross = position["realized"] + position["remaining"] * (exit_price / position["entry_price"] - 1)
                trades.append({"symbol": symbol, "entry": str(position["entry_day"]), "exit": str(day),
                               "net_return_pct": 100 * (gross - 2 * cost_bps / 10_000),
                               "exit_reason": "protective_stop"})
                position = None
            else:
                move_pct = 100 * (bar.close / position["impulse_base"] - 1)
                gain_pct = 100 * (bar.close / position["entry_price"] - 1)
                if move_pct >= config.full_exit_move_pct:
                    gross = position["realized"] + position["remaining"] * (bar.close / position["entry_price"] - 1)
                    trades.append({"symbol": symbol, "entry": str(position["entry_day"]), "exit": str(day),
                                   "net_return_pct": 100 * (gross - 2 * cost_bps / 10_000),
                                   "exit_reason": "mature_impulse_extension"})
                    position = None
                else:
                    if move_pct >= config.scale_out_move_pct and not position["scaled_out"]:
                        position["realized"] += .6 * (bar.close / position["entry_price"] - 1)
                        position["remaining"] -= .6
                        position["scaled_out"] = True
                    trail = sma(rows, index, 20 if move_pct < 40 else 10)
                    candidates = [stop]
                    if gain_pct >= config.break_even_profit_pct:
                        candidates.append(position["entry_price"])
                    if trail is not None:
                        candidates.append(trail)
                    position["stop"] = max(candidates)

        if position is None and pending_entry and index == pending_entry["index"]:
            breakout = pending_entry
            entry_gap = 100 * (bar.open / breakout["close"] - 1)
            risk = 100 * (bar.open / breakout["stop"] - 1)
            extension = None
            average20 = sma(rows, index - 1, 20)
            if average20:
                extension = 100 * (bar.open / average20 - 1)
            if (bar.open >= breakout["setup_high"] and entry_gap <= max_entry_gap
                    and 0 < risk <= max_initial_risk and extension is not None
                    and extension <= max_entry_sma20_extension):
                position = {"entry_day": day, "entry_price": bar.open,
                            "stop": breakout["stop"], "impulse_base": breakout["impulse_base"],
                            "remaining": 1.0, "realized": 0.0, "scaled_out": False}
            pending_entry = None

        if position is None and setup and index == setup["index"] + 1:
            if entry_mode == "sma20_pullback_reversal":
                entry_gap = 100 * (bar.open / setup["setup_close"] - 1)
                risk = 100 * (bar.open / setup["reversal_stop"] - 1)
                if (entry_gap <= max_entry_gap and bar.open >= setup["reversal_stop"]
                        and 0 < risk <= max_initial_risk):
                    if bar.low <= setup["reversal_stop"]:
                        gross = setup["reversal_stop"] / bar.open - 1
                        trades.append({"symbol": symbol, "entry": str(day), "exit": str(day),
                                       "net_return_pct": 100 * (gross - 2 * cost_bps / 10_000),
                                       "exit_reason": "entry_session_stop_daily_bar_proxy"})
                    else:
                        position = {"entry_day": day, "entry_price": bar.open,
                                    "stop": setup["reversal_stop"],
                                    "impulse_base": setup["impulse_base"],
                                    "remaining": 1.0, "realized": 0.0, "scaled_out": False}
                setup = None
                continue
            if entry_mode == "setup_high_buy_stop":
                trigger = max(bar.open, setup["setup_high"])
                opening_gap = 100 * (bar.open / setup["setup_high"] - 1)
                risk = 100 * (trigger / setup["setup_low"] - 1)
                average20 = sma(rows, index - 1, 20)
                extension = 100 * (trigger / average20 - 1) if average20 else 999
                if (bar.high > setup["setup_high"] and opening_gap <= max_breakout_gap
                        and 0 < risk <= max_initial_risk
                        and extension <= max_entry_sma20_extension):
                    if bar.low <= setup["setup_low"]:
                        gross = setup["setup_low"] / trigger - 1
                        trades.append({"symbol": symbol, "entry": str(day), "exit": str(day),
                                       "net_return_pct": 100 * (gross - 2 * cost_bps / 10_000),
                                       "exit_reason": "entry_session_stop_daily_bar_proxy"})
                    else:
                        position = {"entry_day": day, "entry_price": trigger,
                                    "stop": setup["setup_low"],
                                    "impulse_base": setup["impulse_base"],
                                    "remaining": 1.0, "realized": 0.0, "scaled_out": False}
                setup = None
                continue
            average_volume = mean(item.volume for _, item in rows[index - 20:index])
            location = (bar.close - bar.low) / (bar.high - bar.low) if bar.high > bar.low else 0
            breakout_gap = 100 * (bar.open / setup["setup_high"] - 1)
            if (bar.high > setup["setup_high"] and bar.close > setup["setup_high"]
                    and bar.low > setup["setup_low"]
                    and location >= breakout_close_location
                    and average_volume > 0 and bar.volume / average_volume >= breakout_volume_ratio
                    and breakout_gap <= max_breakout_gap):
                pending_entry = {"index": index + 1, "close": bar.close,
                                 "stop": setup["setup_low"],
                                 "setup_high": setup["setup_high"],
                                 "impulse_base": setup["impulse_base"]}
            setup = None

        if position is None and pending_entry is None:
            values = setup_features(rows, index, focus_index, config)
            if values and values[base_rule] and values["focus_age"] <= max_focus_age:
                if values["pullback_pct"] >= min_pullback and index + 2 < len(rows):
                    setup = {**values, "index": index}
    return trades


def metrics(rows):
    values = [row["net_return_pct"] for row in rows]
    wins = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value <= 0)
    return {"trades": len(values),
            "win_rate_pct": round(100 * sum(value > 0 for value in values) / len(values), 2) if values else None,
            "average_net_trade_pct": round(mean(values), 4) if values else None,
            "sum_net_trade_pct": round(sum(values), 3),
            "profit_factor": round(wins / losses, 4) if losses else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    symbols = load_equity_universe(("midcap150", "smallcap500"))
    data = load_database_data(symbols, date(2020, 1, 1), date(2026, 11, 14),
                              database_only=True, skip_missing_instruments=True)
    results = []
    # Keep the first pass deliberately small and hypothesis-led.  A second
    # grid can refine only a rule that works across every time split.
    variants = product(
        ("sma20_pullback_reversal",), (40, 60), (3.0,),
        (.50,), (1.0,), (2.0,), (1.0, 2.0),
        (4.0, 6.0), (8.0,), ("sma20_pullback_reversal",),
    )
    for values in variants:
        keys = ("base_rule", "max_focus_age", "min_pullback", "breakout_close_location",
                "breakout_volume_ratio", "max_breakout_gap", "max_entry_gap",
                "max_initial_risk", "max_entry_sma20_extension", "entry_mode")
        rules = dict(zip(keys, values))
        trades = []
        for symbol, rows in data.items():
            if rows:
                trades.extend(candidate_trades(symbol, rows, **rules))
        splits = {name: metrics([row for row in trades
                                 if start <= date.fromisoformat(row["entry"]) <= end])
                  for name, (start, end) in SPLITS.items()}
        averages = [value["average_net_trade_pct"] for value in splits.values()]
        factors = [value["profit_factor"] for value in splits.values()]
        if min(value["trades"] for value in splits.values()) >= 40 and all(
                value is not None for value in averages + factors):
            score = min(averages) + .1 * min(factors)
            results.append({"rules": rules, "all": metrics(trades), "splits": splits,
                            "robust_score": round(score, 6)})
    results.sort(key=lambda row: row["robust_score"], reverse=True)
    payload = {"variants_retained": len(results), "top_variants": results[:30]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2))
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
