"""Run the same decision engine used by live closed-candle callbacks.

CSV must contain 15-minute NIFTY index OHLC and a date column. Results are
index-point signals, not option P&L (which needs historical option premiums).
"""

import argparse
import csv
import logging
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import NamedTuple

from strategies.nifty_15m_breakout import Bar, Nifty15mBreakout


class TradeResult(NamedTuple):
    side: str
    entry_time: object
    exit_time: object
    entry_price: Decimal
    exit_price: Decimal
    stop: Decimal
    exit_reason: str
    points: Decimal
    initial_risk: Decimal


def completed_trades(decisions, fill_prices=None, slippage_points=0):
    """Pair entry and exit signals; retain an unfinished entry separately."""
    slippage = Decimal(str(slippage_points))
    if slippage < 0:
        raise ValueError("Slippage must be nonnegative")
    trades = []
    entry = None
    for decision in decisions:
        if decision.action.startswith("BUY_"):
            if entry is not None:
                raise ValueError("Entry signal while a trade is already open")
            entry = decision
        elif decision.action.startswith("EXIT_"):
            if entry is None or decision.action[-2:] != entry.action[-2:]:
                raise ValueError("Exit signal without a matching entry")
            direction = Decimal(1) if entry.action == "BUY_CE" else Decimal(-1)
            if fill_prices is None:
                entry_price, exit_price = entry.index_price, decision.index_price
            else:
                if entry.time not in fill_prices or decision.time not in fill_prices:
                    raise ValueError("Next-candle open missing for a signal; use complete session data")
                entry_price, exit_price = fill_prices[entry.time], fill_prices[decision.time]
            entry_price += direction * slippage
            exit_price -= direction * slippage
            initial_risk = direction * (entry_price - entry.stop)
            if initial_risk <= 0:
                raise ValueError("Entry fill crossed the stop; this trade cannot be simulated safely")
            trades.append(TradeResult(
                side=entry.action[-2:], entry_time=entry.time, exit_time=decision.time,
                entry_price=entry_price, exit_price=exit_price,
                stop=entry.stop, exit_reason=decision.reason,
                points=direction * (exit_price - entry_price),
                initial_risk=initial_risk,
            ))
            entry = None
    return trades, entry


def summarize(trades):
    """All P&L figures are NIFTY index points, never option premium or rupees."""
    zero = Decimal(0)
    winners = [trade for trade in trades if trade.points > 0]
    losers = [trade for trade in trades if trade.points < 0]
    gross_profit = sum((trade.points for trade in winners), zero)
    gross_loss = -sum((trade.points for trade in losers), zero)
    equity = peak = zero
    max_drawdown = zero
    for trade in trades:
        equity += trade.points
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    count = len(trades)
    return {
        "total": count,
        "wins": len(winners),
        "losses": len(losers),
        "breakeven": count - len(winners) - len(losers),
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "net_points": gross_profit - gross_loss,
        "win_rate": Decimal(100) * len(winners) / count if count else zero,
        "average_win": gross_profit / len(winners) if winners else zero,
        "average_loss": gross_loss / len(losers) if losers else zero,
        "expectancy": (gross_profit - gross_loss) / count if count else zero,
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "payoff_ratio": ((gross_profit / len(winners)) / (gross_loss / len(losers))
                         if winners and losers else None),
        "max_drawdown": max_drawdown,
        "average_initial_risk": (sum((trade.initial_risk for trade in trades), zero) / count
                                 if count else zero),
        "average_r": (sum((trade.points / trade.initial_risk for trade in trades), zero) / count
                      if count else zero),
    }


def print_summary(trades, open_entry, *, fill_model="signal-close", slippage_points=0,
                  skipped_risk=0):
    stats = summarize(trades)

    def points(value):
        return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}"

    def ratio(value):
        return f"{value:.2f}" if value is not None else "N/A"

    print("\nBACKTEST SUMMARY — NIFTY INDEX POINTS")
    print(f"Fill model: {fill_model} | Assumed slippage: {slippage_points} points per side "
          f"| Risk-cap skips: {skipped_risk}")
    print(f"Completed trades: {stats['total']} | Open signal: {'yes' if open_entry else 'no'}")
    print(f"Wins: {stats['wins']} | Losses: {stats['losses']} | Breakeven: {stats['breakeven']} "
          f"| Win rate: {stats['win_rate']:.2f}%")
    print(f"Gross profit: +{points(stats['gross_profit'])} | Gross loss: -{points(stats['gross_loss'])} "
          f"| Net: {points(stats['net_points'])}")
    print(f"Average win: +{points(stats['average_win'])} | Average loss: -{points(stats['average_loss'])} "
          f"| Expectancy/trade: {points(stats['expectancy'])}")
    print(f"Profit factor: {ratio(stats['profit_factor'])} | Payoff ratio: {ratio(stats['payoff_ratio'])} "
          f"| Average initial risk: {points(stats['average_initial_risk'])}")
    print(f"Average R/trade: {ratio(stats['average_r'])} | Max closed-trade drawdown: "
          f"{points(stats['max_drawdown'])}")
    for side in ("CE", "PE"):
        group = [trade for trade in trades if trade.side == side]
        side_stats = summarize(group)
        print(f"{side} trades: {len(group)} | {side_stats['wins']} wins | "
              f"{side_stats['losses']} losses | Win rate {side_stats['win_rate']:.2f}%")
        print(f"  Gross profit +{points(side_stats['gross_profit'])} | "
              f"Gross loss -{points(side_stats['gross_loss'])} | "
              f"Net {points(side_stats['net_points'])}")
        print(f"  Expectancy/trade {points(side_stats['expectancy'])} | "
              f"Profit factor {ratio(side_stats['profit_factor'])} | "
              f"Max closed-trade drawdown {points(side_stats['max_drawdown'])}")
    for reason in ("target", "stop", "session_end"):
        group = [trade for trade in trades if trade.exit_reason == reason]
        if group:
            reason_stats = summarize(group)
            print(f"{reason}: {len(group)} exits | Net {points(reason_stats['net_points'])} points")
    print("Index-point proxy only; excludes option premiums, brokerage and taxes. "
          "Next-open fills and slippage are assumptions.")
    final_points = stats["net_points"].quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    print("=" * 56)
    print(f"FINAL P&L (NIFTY INDEX POINTS, NOT ₹): {final_points:+,.2f}")
    print("=" * 56)


def run(rows, *, max_risk_points=30, include_holds=False):
    engine = Nifty15mBreakout(max_risk_points=max_risk_points)
    return [decision for row in rows if (decision := engine.on_bar(Bar(
        _time(row["date"]), *(Decimal(str(row[key])) for key in ("open", "high", "low", "close"))
    ))).action != "HOLD" or include_holds]


def next_open_fills(rows):
    """Map each signal candle to the following bar's open within that session."""
    fills = {}
    for current, following in zip(rows, rows[1:]):
        current_time, next_time = _time(current["date"]), _time(following["date"])
        if (isinstance(current_time, datetime) and isinstance(next_time, datetime)
                and current_time.date() == next_time.date()
                and next_time - current_time == timedelta(minutes=15)):
            fills[current_time] = Decimal(str(following["open"]))
    return fills


def _time(value):
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)


def fetch_from_kite(start, end, *, interval="15minute"):
    from nifty_15m_env import load_nifty_environment
    load_nifty_environment()

    from trading.user_token import fetch_user_token
    kite, _user_id = fetch_user_token(logging.getLogger(__name__))
    nifty = next((item for item in kite.instruments("NSE")
                  if item["tradingsymbol"] == "NIFTY 50"), None)
    if nifty is None:
        raise ValueError("NIFTY 50 not found in Kite instruments")
    rows = []
    day = start
    while day <= end:
        chunk_end = min(day + timedelta(days=29), end)
        for record in kite.historical_data(nifty["instrument_token"],
                                           f"{day} 09:15:00", f"{chunk_end} 15:30:00", interval):
            rows.append({"date": record["date"].isoformat(),
                         **{key: record[key] for key in ("open", "high", "low", "close")}})
        day = chunk_end + timedelta(days=1)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", nargs="?", help="15-minute NIFTY index OHLC CSV")
    parser.add_argument("--fetch", action="store_true", help="Fetch 15-minute NIFTY candles from Kite")
    parser.add_argument("--from-date", help="Start date YYYY-MM-DD for --fetch")
    parser.add_argument("--to-date", help="End date YYYY-MM-DD for --fetch")
    parser.add_argument("--summary-only", action="store_true", help="Show summary without individual signals")
    risk_group = parser.add_mutually_exclusive_group()
    risk_group.add_argument("--max-risk-points", type=Decimal, default=Decimal(30),
                            help="Maximum signal-close-to-stop distance (default: 30)")
    risk_group.add_argument("--no-risk-cap", dest="max_risk_points", action="store_const", const=None,
                            help="Run the uncapped baseline")
    parser.add_argument("--fill-model", choices=("next-open", "signal-close"), default="next-open",
                        help="Index-price fill assumption; signal-close reproduces the old report")
    parser.add_argument("--slippage-points", type=Decimal, default=Decimal(0),
                        help="Adverse NIFTY-point adjustment on each entry and exit fill")
    args = parser.parse_args()
    if args.max_risk_points is not None and args.max_risk_points <= 0:
        parser.error("--max-risk-points must be positive")
    if args.slippage_points < 0:
        parser.error("--slippage-points must be nonnegative")
    if args.fetch:
        if args.csv_path or not args.from_date or not args.to_date:
            parser.error("--fetch requires --from-date and --to-date, without a CSV path")
        try:
            start = datetime.fromisoformat(args.from_date).date()
            end = datetime.fromisoformat(args.to_date).date()
            if end < start:
                parser.error("--to-date must be on or after --from-date")
            rows = fetch_from_kite(start, end)
        except ValueError as exc:
            parser.error(str(exc))
    else:
        if not args.csv_path:
            parser.error("Provide a CSV path or use --fetch")
        with open(args.csv_path, newline="") as source:
            rows = list(csv.DictReader(source))
    if len(rows) < 2:
        parser.error("At least two 15-minute candles are required")
    try:
        times = [datetime.fromisoformat(row["date"]) for row in rows]
    except (KeyError, ValueError) as exc:
        parser.error(f"Invalid date column: {exc}")
    if any(b <= a for a, b in zip(times, times[1:])):
        parser.error("Candles must be unique and ordered oldest to newest")
    same_day_gaps = [b - a for a, b in zip(times, times[1:]) if a.date() == b.date()]
    if not same_day_gaps or any(gap != timedelta(minutes=15) for gap in same_day_gaps):
        parser.error("CSV must contain consecutive 15-minute candles within each session")
    all_decisions = run(rows, max_risk_points=args.max_risk_points, include_holds=True)
    skipped_risk = sum(decision.reason == "risk_cap" for decision in all_decisions)
    decisions = [decision for decision in all_decisions if decision.action != "HOLD"]
    fills = next_open_fills(rows) if args.fill_model == "next-open" else None
    try:
        trades, open_entry = completed_trades(decisions, fills, args.slippage_points)
    except ValueError as exc:
        parser.error(str(exc))
    if not args.summary_only:
        print("time,action,nifty_close,stop,target,reason")
        for item in decisions:
            print(f"{item.time},{item.action},{item.index_price},{item.stop or ''},{item.target or ''},{item.reason}")
    if args.max_risk_points is not None:
        baseline_decisions = run(rows, max_risk_points=None)
        try:
            baseline_trades, _ = completed_trades(
                baseline_decisions, fills, args.slippage_points)
        except ValueError as exc:
            parser.error(str(exc))
        baseline = summarize(baseline_trades)
        selected = summarize(trades)
        print("\nSAME-DATA COMPARISON — INDEX POINTS")
        print(f"No risk cap: {baseline['total']} trades | Net {baseline['net_points']:.2f} "
              f"| Expectancy {baseline['expectancy']:.2f}/trade")
        print(f"{args.max_risk_points}-point cap: {selected['total']} trades | "
              f"Net {selected['net_points']:.2f} | Expectancy {selected['expectancy']:.2f}/trade")
        print("This comparison uses the same sample; it does not establish out-of-sample performance.")
    print_summary(trades, open_entry, fill_model=args.fill_model,
                  slippage_points=args.slippage_points, skipped_risk=skipped_risk)


if __name__ == "__main__":
    main()
