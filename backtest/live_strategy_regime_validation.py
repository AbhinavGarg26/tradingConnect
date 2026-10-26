"""Validate the six deployed strategies over baseline and recent market windows.

This is a NIFTY-index signal comparison. Historical option premiums, spreads,
brokerage, taxes, contract selection and the live ₹30,000 premium cap are not
available in the source history and are therefore not reconstructed.
"""
import argparse
import csv
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from backtest.fixed_profit import load_sessions, statistics
from backtest.fixed_profit_filtered import replay as replay_fixed_profit
from backtest.nifty_15m_breakout import completed_trades, next_open_fills
from backtest.nifty_15m_breakout import run as run_breakout
from backtest.nifty_15m_breakout import summarize as summarize_intraday
from backtest.nifty_30m_put_comparison import run as run_bearish_ema
from backtest.nifty_30m_reference import run as run_reference
from backtest.nifty_bullish_ema_timeframes import run as run_bullish_ema

IST = ZoneInfo("Asia/Kolkata")
WINDOWS = (
    ("baseline", date(2020, 1, 1)),
    ("last_1_year", date(2025, 10, 24)),
    ("last_6_months", date(2026, 4, 24)),
)


def read_csv(path):
    with Path(path).open(newline="") as source:
        return list(csv.DictReader(source))


def local_day(value):
    stamp = datetime.fromisoformat(str(value))
    if stamp.tzinfo is None:
        return stamp.date()
    return stamp.astimezone(IST).date()


def point_stats(trades):
    stats = summarize_intraday(trades)
    return {
        "trades": stats["total"],
        "win_rate_pct": float(stats["win_rate"]),
        "net_index_points": float(stats["net_points"]),
        "expectancy_points": float(stats["expectancy"]),
        "profit_factor": float(stats["profit_factor"]) if stats["profit_factor"] is not None else None,
        "max_drawdown_points": float(stats["max_drawdown"]),
    }


def ema_stats(report):
    stats = report["stats"]
    return {
        "trades": stats["closed_trades"],
        "win_rate_pct": stats["win_rate_pct"] or 0,
        "net_index_points": stats["net_index_points"],
        "expectancy_points": stats["net_index_points"] / stats["closed_trades"] if stats["closed_trades"] else 0,
        "profit_factor": stats["profit_factor"],
        "max_drawdown_points": report["max_marked_drawdown_points"],
        "open_position": bool(report["open_position"]),
        "marked_net_index_points": report["total_marked_points"],
    }


def classification(stats, *, minimum_trades):
    if stats["trades"] < minimum_trades:
        return "INSUFFICIENT_SAMPLE"
    if stats["net_index_points"] > 0 and (stats["profit_factor"] or 0) > 1:
        return "POSITIVE"
    return "WEAK"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minute-source", default="outputs/fixed-profit-36-100/nifty_minute_source.csv")
    parser.add_argument("--candles-15m", default="outputs/nifty_15m_put_comparison/candles_15m.csv")
    parser.add_argument("--daily-source", default="outputs/nifty_daily_ema_comparison/daily_input.csv")
    parser.add_argument("--output-dir", default="outputs/live-strategy-regime-validation-20261025")
    args = parser.parse_args()

    candles_15m = read_csv(args.candles_15m)
    daily_rows = read_csv(args.daily_source)
    end = max(local_day(row["date"]) for row in candles_15m)
    sessions, excluded = load_sessions(args.minute_source)

    # Reuse the validated complete-session minute bars. The raw snapshot stores
    # naive UTC, while the reference replay requires explicit exchange time.
    reference_rows = [
        {"date": bar.time.isoformat(), "open": bar.open, "high": bar.high,
         "low": bar.low, "close": bar.close}
        for _, bars in sessions for bar in bars
    ]

    strategies = {
        "nifty_15m_breakout": {"name": "Nifty 15m Breakout v3", "minimum_trades": 10, "windows": {}},
        "nifty_30m_reference": {"name": "NIFTY 30-minute PE1-V1", "minimum_trades": 10, "windows": {}},
        "fixed_profit_filtered": {"name": "Fixed Profit · Filtered", "minimum_trades": 10, "windows": {}},
        "nifty_daily_ema_monthly": {"name": "Daily EMA 20/50 · Calls", "minimum_trades": 2, "windows": {}},
        "nifty_bearish_15m_9_21": {"name": "15m EMA 9/21 · Puts", "minimum_trades": 5, "windows": {}},
        "nifty_bullish_15m_9_21": {"name": "15m EMA 9/21 · Calls", "minimum_trades": 5, "windows": {}},
    }

    for label, start in WINDOWS:
        start_text = start.isoformat()

        breakout_rows = [row for row in candles_15m if local_day(row["date"]) >= start]
        decisions = run_breakout(breakout_rows, max_risk_points=Decimal("30"))
        trades, open_entry = completed_trades(decisions, next_open_fills(breakout_rows), Decimal("1"))
        stats = point_stats(trades) | {"open_signal": open_entry is not None}
        strategies["nifty_15m_breakout"]["windows"][label] = stats

        reference_window = [row for row in reference_rows if local_day(row["date"]) >= start]
        trades, entry, pending, _, _ = run_reference(
            reference_window, stop_points=25, max_entries=1, buffer_percent=5,
            slippage_points=1, first_signal_pe=True,
        )
        stats = point_stats(trades) | {"open_position": entry is not None, "pending_signal": pending is not None}
        strategies["nifty_30m_reference"]["windows"][label] = stats

        fixed_trades = [trade for day, bars in sessions if day >= start and (trade := replay_fixed_profit(bars))]
        fixed_stats = statistics(fixed_trades, 2)
        strategies["fixed_profit_filtered"]["windows"][label] = {
            "trades": fixed_stats["trades"], "win_rate_pct": fixed_stats["win_pct"],
            "net_index_points": fixed_stats["net_points"], "expectancy_points": fixed_stats["avg_points"],
            "profit_factor": fixed_stats["profit_factor"], "max_drawdown_points": fixed_stats["max_drawdown_points"],
        }

        # EMA functions retain the full 2020 history for indicator warm-up and
        # explicitly start flat at the requested evaluation boundary.
        daily_start = start_text if label != "baseline" else "2020-03-17"
        report, _, _, _ = run_bullish_ema(daily_rows, 20, 50, timeframe="1d", evaluation_start=daily_start)
        strategies["nifty_daily_ema_monthly"]["windows"][label] = ema_stats(report)

        intraday_ema_start = start_text if label != "baseline" else "2020-01-02"
        report, _, _, _ = run_bearish_ema(candles_15m, 9, 21, evaluation_start=intraday_ema_start, timeframe_minutes=15)
        strategies["nifty_bearish_15m_9_21"]["windows"][label] = ema_stats(report)

        report, _, _, _ = run_bullish_ema(candles_15m, 9, 21, timeframe="15m", evaluation_start=intraday_ema_start)
        strategies["nifty_bullish_15m_9_21"]["windows"][label] = ema_stats(report)

    for strategy in strategies.values():
        minimum = strategy["minimum_trades"]
        for stats in strategy["windows"].values():
            stats["assessment"] = classification(stats, minimum_trades=minimum)
        recent = [strategy["windows"]["last_1_year"], strategy["windows"]["last_6_months"]]
        strategy["recent_environment"] = (
            "PASS" if all(row["assessment"] == "POSITIVE" for row in recent)
            else "FAIL" if any(row["assessment"] == "WEAK" for row in recent)
            else "INCONCLUSIVE"
        )

    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "data_start": "2020-01-01", "data_end": str(end),
        "windows": {label: {"start": str(start), "end": str(end)} for label, start in WINDOWS},
        "excluded_incomplete_minute_sessions": excluded,
        "method": {
            "frozen_rules": True,
            "intraday_slippage_points_per_fill": 1,
            "ema_slippage_points_per_fill": 0.5,
            "fixed_profit_round_trip_cost_points": 2,
            "recent_pass_rule": "Both trailing windows must have positive net points, profit factor above 1, and the minimum closed-trade sample.",
            "limitations": "NIFTY index-point proxy; not historical option P&L. EMA windows start flat and wait for a fresh crossover. Baseline 15m EMA evaluation starts 2020-01-02 after one warm-up session; daily EMA starts 2020-03-17 after 50-session warm-up.",
        },
        "strategies": strategies,
    }
    (output / "results.json").write_text(json.dumps(report, indent=2, default=str, allow_nan=False))

    lines = [
        "# Six live strategies: baseline and recent-regime validation", "",
        f"Data: 1 January 2020 through {end.strftime('%-d %B %Y')}. Production rules were not tuned.", "",
        "| Strategy | Window | Trades | Win rate | Net points | PF | Max DD | Assessment |", "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for strategy in strategies.values():
        for label, _ in WINDOWS:
            row = strategy["windows"][label]
            pf = f"{row['profit_factor']:.2f}" if row["profit_factor"] is not None else "N/A"
            lines.append(f"| {strategy['name']} | {label.replace('_', ' ')} | {row['trades']} | {row['win_rate_pct']:.2f}% | {row['net_index_points']:+.2f} | {pf} | {row['max_drawdown_points']:.2f} | {row['assessment']} |")
        lines.append(f"| **{strategy['name']}** | **Recent verdict** |  |  |  |  |  | **{strategy['recent_environment']}** |")
    lines += ["", "## Interpretation", "", "PASS requires both recent windows to be positive with PF above 1 and enough closed trades. INCONCLUSIVE means the strategy traded too rarely for a defensible recent-window claim. FAIL means at least one sufficiently sampled recent window was weak.", "", "Results are NIFTY index-point signal proxies, not executable option returns. Historical option premiums, bid/ask spreads, brokerage, taxes, monthly-contract selection and the ₹30,000 premium cap are not modeled."]
    (output / "report.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
