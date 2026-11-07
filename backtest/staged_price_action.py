"""Backtest one-lot probe followed by a configurable confirmed addition.

Input is one-minute NIFTY OHLC. Output is index points and quantity-weighted
lot-points; it is not an option-premium backtest.
"""
import argparse
import csv
import json
from datetime import time, timedelta
from pathlib import Path

from backtest.fixed_profit import aggregate, load_sessions


def _stats(trades, cost_per_lot=0):
    pnls = [t["lot_points"] - cost_per_lot * t["round_trip_lots"] for t in trades]
    equity = peak = max_drawdown = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    gains = sum(x for x in pnls if x > 0)
    losses = -sum(x for x in pnls if x < 0)
    return {
        "trades": len(trades),
        "confirmed_trades": sum(t["confirmed"] for t in trades),
        "win_pct": round(100 * sum(x > 0 for x in pnls) / len(pnls), 2) if pnls else 0,
        "net_lot_points": round(sum(pnls), 2),
        "avg_lot_points": round(sum(pnls) / len(pnls), 3) if pnls else 0,
        "profit_factor": round(gains / losses, 3) if losses else None,
        "max_drawdown_lot_points": round(max_drawdown, 2),
    }


def run_session(bars, *, add_lots=2):
    if not isinstance(add_lots, int) or add_lots < 1:
        raise ValueError("add_lots must be a positive integer")
    reference = aggregate(bars[:30])
    if reference.high - reference.low <= 40:
        return None
    probe = add = target = stop = None
    side = level = breakout_extreme = None
    pullback_seen = confirmed = False
    pending_probe = pending_add = None
    entry_time = confirmation_time = None

    for i in range(30, len(bars)):
        minute = bars[i]
        now = minute.time.time()
        if pending_probe:
            side, level, breakout_extreme = pending_probe
            direction = 1 if side == "CE" else -1
            probe = minute.open
            stop = level - direction * 5
            entry_time = minute.time
            pending_probe = None
        if pending_add:
            add = minute.open
            confirmation_time = minute.time
            confirmed = True
            # The probe becomes protected; the add carries the remaining risk.
            stop = probe
            target = add + (10 if side == "CE" else -10)
            pending_add = False

        if probe is not None:
            direction = 1 if side == "CE" else -1
            if now >= time(15, 15):
                exit_price, reason = minute.open, "session_end"
            elif (minute.low <= stop if direction == 1 else minute.high >= stop):
                exit_price = min(minute.open, stop) if direction == 1 else max(minute.open, stop)
                reason = "stop"
            elif confirmed and (minute.high >= target if direction == 1 else minute.low <= target):
                exit_price, reason = target, "target"
            else:
                exit_price = reason = None
            if reason:
                probe_points = direction * (exit_price - probe)
                add_points = direction * (exit_price - add) if confirmed else 0
                return {
                    "date": str(minute.time.date()), "side": side,
                    "entry_time": entry_time.isoformat(),
                    "confirmation_time": confirmation_time.isoformat() if confirmation_time else "",
                    "probe_entry": float(probe), "add_entry": float(add) if add else "",
                    "exit_time": minute.time.isoformat(), "exit": float(exit_price),
                    "exit_reason": reason, "confirmed": confirmed,
                    "probe_points": float(probe_points), "add_points": float(add_points),
                    "lot_points": float(probe_points + add_lots * add_points),
                    "round_trip_lots": 1 + add_lots if confirmed else 1,
                }

        # Decisions use only completed, clock-aligned five-minute bars.
        if (i - 29) % 5 != 0:
            continue
        candle = aggregate(bars[i-4:i+1])
        candle_end = minute.time + timedelta(minutes=1)
        if probe is None and candle_end.time() < time(12, 30):
            if (candle.close > reference.high and candle.close > candle.open
                    and candle.close <= reference.high + 5):
                pending_probe = ("CE", reference.high, candle.high)
            elif (candle.close < reference.low and candle.close < candle.open
                    and candle.close >= reference.low - 5):
                pending_probe = ("PE", reference.low, candle.low)
        elif probe is not None and not confirmed and candle_end.time() < time(12, 30):
            direction = 1 if side == "CE" else -1
            if not pullback_seen:
                touches = candle.low <= level + 2 if direction == 1 else candle.high >= level - 2
                holds = candle.close >= level if direction == 1 else candle.close <= level
                if touches and holds:
                    pullback_seen = True
            elif (candle.close > breakout_extreme if direction == 1 else candle.close < breakout_extreme):
                pending_add = True
    return None


def analyze(source, output, *, add_lots=2):
    sessions, excluded = load_sessions(source)
    trades = [trade for _, bars in sessions if (trade := run_session(bars, add_lots=add_lots))]
    years = range(sessions[0][0].year, sessions[-1][0].year + 1)
    report = {
        "strategy": f"staged_price_action_probe_1_add_{add_lots}_v1",
        "data_basis": "NIFTY one-minute OHLC; no option premiums",
        "rules": {
            "opening_range": "09:15-09:45, greater than 40 points",
            "probe": "one lot; five-minute directional close outside but within 5 points of range",
            "confirmation": "later pullback within 2 points holds, then later close beyond breakout extreme",
            "addition": f"{add_lots} lots at next one-minute open, before 12:30",
            "initial_stop": "5 points inside range",
            "confirmed_stop": "probe entry (breakeven for probe)",
            "target": "10 NIFTY points from addition",
            "same_bar_order": "stop before target",
        },
        "first_date": str(sessions[0][0]), "last_date": str(sessions[-1][0]),
        "complete_sessions": len(sessions), "excluded_dates": excluded,
        "all": {"gross": _stats(trades), "cost_1_point_per_lot": _stats(trades, 1),
                "cost_2_points_per_lot": _stats(trades, 2)},
        "yearly_cost_1_point_per_lot": {
            str(year): _stats([t for t in trades if t["date"].startswith(str(year))], 1)
            for year in years
        },
        "by_side_cost_1_point_per_lot": {
            side: _stats([t for t in trades if t["side"] == side], 1) for side in ("CE", "PE")
        },
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
    parser.add_argument("source")
    parser.add_argument("--output", default="outputs/staged-price-action")
    parser.add_argument("--add-lots", type=int, default=2)
    args = parser.parse_args()
    analyze(args.source, args.output, add_lots=args.add_lots)
