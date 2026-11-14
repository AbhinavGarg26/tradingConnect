"""Executable-price backtest for Profit Lock; refuses index-only proxy data."""
import argparse
import csv
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True)
class Charges:
    brokerage_per_order: Decimal
    stt_sell_pct: Decimal
    exchange_pct: Decimal
    sebi_pct: Decimal
    stamp_buy_pct: Decimal
    gst_pct: Decimal
    slippage_ticks: Decimal
    tick_size: Decimal


def evaluate(rows, charges: Charges):
    trades = []
    for row in rows:
        required = ("entry_ask", "exit_bid", "quantity", "entry_time", "exit_time",
                    "symbol", "expiry", "strike", "side")
        if any(row.get(name) in (None, "") for name in required):
            raise ValueError("Historical option bid/ask rows are incomplete; index proxies are forbidden")
        entry = Decimal(row["entry_ask"]) + charges.slippage_ticks * charges.tick_size
        exit_price = Decimal(row["exit_bid"]) - charges.slippage_ticks * charges.tick_size
        quantity = int(row["quantity"])
        if entry <= 0 or exit_price <= 0 or quantity <= 0:
            raise ValueError("Option prices and quantity must be positive")
        buy_value, sell_value = entry * quantity, exit_price * quantity
        brokerage = charges.brokerage_per_order * 2
        exchange = (buy_value + sell_value) * charges.exchange_pct / Decimal(100)
        sebi = (buy_value + sell_value) * charges.sebi_pct / Decimal(100)
        gst = (brokerage + exchange + sebi) * charges.gst_pct / Decimal(100)
        stt = sell_value * charges.stt_sell_pct / Decimal(100)
        stamp = buy_value * charges.stamp_buy_pct / Decimal(100)
        costs = brokerage + exchange + sebi + gst + stt + stamp
        gross = (exit_price - entry) * quantity
        trades.append({"symbol": row["symbol"], "entry_time": row["entry_time"],
                       "exit_time": row["exit_time"], "gross": float(gross),
                       "costs": float(costs), "net": float(gross - costs)})
    wins = [trade["net"] for trade in trades if trade["net"] > 0]
    losses = [-trade["net"] for trade in trades if trade["net"] < 0]
    net = sum(trade["net"] for trade in trades)
    return {"status": "complete", "data_basis": "historical_option_bid_ask",
            "trades": len(trades), "net_rupees": round(net, 2),
            "profit_factor": round(sum(wins) / sum(losses), 3) if losses else None,
            "accepted": len(trades) >= 30 and net > 0 and bool(losses) and sum(wins) / sum(losses) >= 1.05,
            "details": trades}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source"); parser.add_argument("--output", required=True)
    for name in ("brokerage-per-order", "stt-sell-pct", "exchange-pct", "sebi-pct",
                 "stamp-buy-pct", "gst-pct", "slippage-ticks", "tick-size"):
        parser.add_argument("--" + name, type=Decimal, required=True)
    args = parser.parse_args()
    with open(args.source) as handle: rows = list(csv.DictReader(handle))
    report = evaluate(rows, Charges(*(getattr(args, name.replace("-", "_")) for name in (
        "brokerage-per-order", "stt-sell-pct", "exchange-pct", "sebi-pct",
        "stamp-buy-pct", "gst-pct", "slippage-ticks", "tick-size"))))
    Path(args.output).write_text(json.dumps(report, indent=2)); print(json.dumps(report, indent=2))


if __name__ == "__main__": main()
