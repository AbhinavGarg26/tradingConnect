"""
backtest/adx_backtest.py  v4
=============================
Exit mechanism — designed to solve the 0.20R winner problem.

THE CORE PROBLEM (from real trade data):
  Winners exited at 0.20R. Losers hit 1.00R.
  Profit factor = 0.17 even at 50% win rate.
  Root cause: trail activated at +1R, SL jumped to breakeven+0.3ATR.
  On next pullback (which always happens) → exit at near-zero.

THE FIX — 3-stage exit with room to breathe:

  Stage 0 — INITIAL (0 to +1R)
    Hard SL at entry − 1.5×ATR.
    No movement. Trade must prove itself first.

  Stage 1 — BREAKEVEN (+1R reached)
    SL moves to entry − 0.25×ATR  (slight buffer below entry, not exactly breakeven)
    Reason: exact breakeven gets hunted by market makers. Give it 0.25×ATR room.
    Still no trailing. Just protecting against a catastrophic loss.

  Stage 2 — TRAIL (+2R reached)
    SL trails at: highest_high − 1.5×ATR  (for BUY)
                  lowest_low  + 1.5×ATR  (for SELL)
    1.5×ATR trail gives a full candle's worth of retracement room.
    Updates every bar as price moves in our favour.
    Does NOT move against us if price reverses.

  Target — 3R fixed.
    At 50% win rate: PF = (3 × 3R) / (3 × 1R) = 3.0  ✓
    If trail catches it early (say at 2.5R), still much better than 0.20R.

  ADX exhaustion — ADX > 40, falling 3 bars → exit at close.
    Prevents giving back trend profits when trend is clearly dying.

Exit priority on same candle: SL/Trail > Target > Exhaust

Usage:
    python backtest/adx_backtest.py --symbol RELIANCE --lookback 500
    python backtest/adx_backtest.py --symbol HDFCBANK --lookback 500 --save-csv
"""

import logging
import argparse
from datetime import datetime, timedelta

import pandas as pd
import numpy as np

from strategies.adx_crossover import ADXCrossoverStrategy

log = logging.getLogger(__name__)


def calculate_performance_metrics(trade_df: pd.DataFrame) -> dict:
    # [Keep this exact function exactly as it was in the previous step]
    if not trade_df.empty:
        total_trades = len(trade_df)
        winning_trades = trade_df[trade_df['PnL (%)'] > 0]
        losing_trades = trade_df[trade_df['PnL (%)'] <= 0]
        win_rate = (len(winning_trades) / total_trades) * 100
        gross_profit = winning_trades['PnL (%)'].sum()
        gross_loss = abs(losing_trades['PnL (%)'].sum())
        net_return = gross_profit - gross_loss
        avg_win = winning_trades['PnL (%)'].mean() if not winning_trades.empty else 0.0
        avg_loss = abs(losing_trades['PnL (%)'].mean()) if not losing_trades.empty else 0.0
        profit_factor = (gross_profit / gross_loss) if gross_loss != 0 else float('inf')
        win_prob = win_rate / 100
        loss_prob = 1 - win_prob
        expectancy = (win_prob * avg_win) - (loss_prob * avg_loss)

        temp_df = trade_df.sort_values(by='Exit Time').copy() if 'Exit Time' in trade_df.columns else trade_df.copy()
        temp_df['Equity_Multiplier'] = 1 + (temp_df['PnL (%)'] / 100)
        temp_df['Cumulative_Equity'] = temp_df['Equity_Multiplier'].cumprod()
        temp_df['High_Water_Mark'] = temp_df['Cumulative_Equity'].cummax()
        temp_df['Drawdown (%)'] = ((temp_df['Cumulative_Equity'] - temp_df['High_Water_Mark']) / temp_df[
            'High_Water_Mark']) * 100
        max_drawdown = temp_df['Drawdown (%)'].min()
    else:
        total_trades = 0
        win_rate = gross_profit = gross_loss = net_return = avg_win = avg_loss = profit_factor = expectancy = max_drawdown = 0.0

    return {
        "Total Trades": total_trades,
        "Win Rate (%)": round(win_rate, 2),
        "Gross Profit (%)": round(gross_profit, 2),
        "Gross Loss (%)": round(gross_loss, 2),
        "Net Return (%)": round(net_return, 2),
        "Average Win (%)": round(avg_win, 2),
        "Average Loss (%)": round(avg_loss, 2),
        "Profit Factor": round(profit_factor, 2) if profit_factor != float('inf') else "INF",
        "Expectancy per Trade (%)": round(expectancy, 2),
        "Max Drawdown (%)": round(max_drawdown, 2) if max_drawdown != 0.0 else 0.0
    }


def run_detailed_backtest(df: pd.DataFrame, stop_loss_pct: float = 0.07, trailing_stop_pct: float = 0.10) -> tuple:
    """
    Simulates trades with dynamic Risk Management (Hard Stop & Trailing Stop).
    """
    df = df.copy()
    trade_logs = []

    active_position = None
    entry_price = 0.0
    entry_time = None
    entry_factors = {}

    # Tracking variables for the Trailing Stop
    favorable_price = 0.0

    for i in range(1, len(df)):
        row = df.iloc[i]
        current_signal = row['signal']
        current_close = row['close']

        if active_position is not None:
            exit_reason = None
            exit_price = 0.0

            # --- RISK MANAGEMENT LOGIC ---
            if active_position == 'LONG':
                favorable_price = max(favorable_price, current_close)  # Update highest peak

                if current_close <= entry_price * (1 - stop_loss_pct):
                    exit_reason = "Hard Stop"
                    exit_price = current_close
                elif current_close <= favorable_price * (1 - trailing_stop_pct):
                    exit_reason = "Trailing Stop"
                    exit_price = current_close
                elif current_signal == -1:
                    exit_reason = "Signal Reversal"
                    exit_price = row['open']

            elif active_position == 'SHORT':
                favorable_price = min(favorable_price, current_close)  # Update lowest trough

                if current_close >= entry_price * (1 + stop_loss_pct):
                    exit_reason = "Hard Stop"
                    exit_price = current_close
                elif current_close >= favorable_price * (1 + trailing_stop_pct):
                    exit_reason = "Trailing Stop"
                    exit_price = current_close
                elif current_signal == 1:
                    exit_reason = "Signal Reversal"
                    exit_price = row['open']

            # --- EXECUTE EXIT ---
            if exit_reason:
                pnl_pct = ((exit_price - entry_price) / entry_price) if active_position == 'LONG' else (
                            (entry_price - exit_price) / entry_price)

                trade_logs.append({
                    "Type": active_position,
                    "Reason": exit_reason,
                    "Entry Time": entry_time,
                    "Entry Price": round(entry_price, 2),
                    "Exit Time": row['date'],
                    "Exit Price": round(exit_price, 2),
                    "PnL (%)": round(pnl_pct * 100, 2)
                })
                active_position = None

        # --- ENTRY LOGIC ---
        if active_position is None and current_signal != 0:
            active_position = 'LONG' if current_signal == 1 else 'SHORT'
            entry_price = row['close']
            entry_time = row['date']
            favorable_price = entry_price  # Initialize trailing stop tracker

    trade_df = pd.DataFrame(trade_logs)
    summary_metrics = calculate_performance_metrics(trade_df)

    return summary_metrics, trade_df


class ADXBacktest:

    def __init__(
        self,
        strategy_kwargs:   dict  = None,
        commission_pct:    float = 0.05,
        # Stage thresholds (in R multiples of initial risk)
        be_activate_r:     float = 1.0,    # move SL to near-breakeven at +1R
        be_buffer_atr:     float = 0.25,   # SL = entry - 0.25×ATR (buffer below entry)
        trail_activate_r:  float = 2.0,    # start trailing at +2R
        trail_atr_mult:    float = 1.5,    # trail = best_price - 1.5×ATR
        # ADX exhaustion
        adx_exhaust_level: float = 40.0,
        adx_exhaust_bars:  int   = 3,
    ):
        self.strategy          = ADXCrossoverStrategy(**(strategy_kwargs or {}))
        self.commission_pct    = commission_pct / 100.0
        self.be_activate_r     = be_activate_r
        self.be_buffer_atr     = be_buffer_atr
        self.trail_activate_r  = trail_activate_r
        self.trail_atr_mult    = trail_atr_mult
        self.adx_exhaust_level = adx_exhaust_level
        self.adx_exhaust_bars  = adx_exhaust_bars

    # ─── Public ──────────────────────────────────────────────────────────────

    def run(self, df: pd.DataFrame, symbol: str = "INSTRUMENT") -> dict:
        signals = self.strategy.run(df)
        if not signals:
            return {"symbol": symbol, "trades": [], "stats": {},
                    "error": "No signals generated"}
        prepared = self.strategy.prepare(df)
        trades   = self._simulate(prepared, signals)
        stats    = self._stats(trades)
        return {
            "symbol":            symbol,
            "signals_generated": len(signals),
            "trades":            trades,
            "stats":             stats,
        }

    # ─── Bar-by-bar simulation ────────────────────────────────────────────────

    def _simulate(self, df: pd.DataFrame, signals: list[dict]) -> list[dict]:
        trades      = []
        active      = None
        sig_by_date = {s["date"]: s for s in signals}
        adx_decline = 0
        prev_adx    = None

        for i in range(len(df)):
            row       = df.iloc[i]
            bar_date  = df.index[i]
            bar_high  = float(row["high"])
            bar_low   = float(row["low"])
            bar_close = float(row["close"])
            bar_adx   = float(row["adx"]) if not pd.isna(row.get("adx", np.nan)) else None

            # ADX exhaustion tracking
            if bar_adx is not None and prev_adx is not None:
                adx_decline = (adx_decline + 1) if bar_adx < prev_adx else 0
            prev_adx = bar_adx
            adx_exhausted = (
                bar_adx is not None
                and bar_adx > self.adx_exhaust_level
                and adx_decline >= self.adx_exhaust_bars
            )

            if active is not None:
                t         = active
                direction = t["signal"]
                atr       = t["atr"]
                risk      = t["risk"]   # initial SL distance = 1.5×ATR

                # Update best price (highest high for BUY, lowest low for SELL)
                if direction == "BUY":
                    t["best_price"] = max(t["best_price"], bar_high)
                else:
                    t["best_price"] = min(t["best_price"], bar_low)

                # Max favourable excursion in R terms
                best_r = (
                    (t["best_price"] - t["entry"]) / risk if direction == "BUY"
                    else (t["entry"] - t["best_price"]) / risk
                )

                # ── Stage 1: Breakeven at +1R ─────────────────────────────
                if not t["be_active"] and best_r >= self.be_activate_r:
                    t["be_active"] = True
                    # SL to entry minus tiny buffer (not hunted at exact breakeven)
                    be_sl = (
                        round(t["entry"] - atr * self.be_buffer_atr, 2) if direction == "BUY"
                        else round(t["entry"] + atr * self.be_buffer_atr, 2)
                    )
                    if direction == "BUY":
                        t["current_sl"] = max(t["current_sl"], be_sl)
                    else:
                        t["current_sl"] = min(t["current_sl"], be_sl)

                # ── Stage 2: Trail at +2R ─────────────────────────────────
                if best_r >= self.trail_activate_r:
                    if not t["trail_active"]:
                        t["trail_active"] = True
                    # Trail updates every bar
                    if direction == "BUY":
                        trail_sl = round(t["best_price"] - atr * self.trail_atr_mult, 2)
                        t["current_sl"] = max(t["current_sl"], trail_sl)
                    else:
                        trail_sl = round(t["best_price"] + atr * self.trail_atr_mult, 2)
                        t["current_sl"] = min(t["current_sl"], trail_sl)

                sl     = t["current_sl"]
                target = t["target"]

                # ── Exit checks ───────────────────────────────────────────
                hit_sl = hit_target = hit_exhaust = False
                if direction == "BUY":
                    if bar_low  <= sl:       hit_sl      = True
                    elif bar_high >= target: hit_target  = True
                    elif adx_exhausted:      hit_exhaust = True
                else:
                    if bar_high >= sl:       hit_sl      = True
                    elif bar_low  <= target: hit_target  = True
                    elif adx_exhausted:      hit_exhaust = True

                if hit_sl or hit_target or hit_exhaust:
                    if hit_sl:
                        exit_price = sl
                        if t["trail_active"]:   exit_reason = "TRAIL_SL"
                        elif t["be_active"]:    exit_reason = "BE_SL"
                        else:                   exit_reason = "SL"
                    elif hit_target:
                        exit_price  = target
                        exit_reason = "TARGET"
                    else:
                        exit_price  = bar_close
                        exit_reason = "ADX_EXHAUST"

                    raw_pnl    = (exit_price - t["entry"]) if direction == "BUY" \
                                 else (t["entry"] - exit_price)
                    commission = (t["entry"] + exit_price) * self.commission_pct
                    net_pnl    = raw_pnl - commission
                    r_multiple = raw_pnl / risk if risk > 0 else 0

                    trades.append({
                        **{k: v for k, v in t.items()
                           if k not in ("current_sl","be_active","trail_active","best_price")},
                        "exit_date":   bar_date,
                        "exit_price":  round(exit_price, 2),
                        "exit_reason": exit_reason,
                        "max_r_seen":  round(best_r, 2),
                        "raw_pnl":     round(raw_pnl, 2),
                        "net_pnl":     round(net_pnl, 2),
                        "r_multiple":  round(r_multiple, 2),
                        "winner":      net_pnl > 0,
                    })
                    active = None

            # ── Open new trade ────────────────────────────────────────────
            if active is None and bar_date in sig_by_date:
                s = sig_by_date[bar_date]
                active = {
                    "signal":        s["signal"],
                    "signal_date":   s["signal_date"],
                    "entry_date":    bar_date,
                    "entry":         s["entry"],
                    "sl":            s["sl"],
                    "current_sl":    s["sl"],
                    "target":        s["target"],
                    "risk":          s["risk"],
                    "atr":           s["atr"],
                    "adx":           s["adx"],
                    "plus_di":       s["plus_di"],
                    "minus_di":      s["minus_di"],
                    "prev_plus_di":  s.get("prev_plus_di"),
                    "prev_minus_di": s.get("prev_minus_di"),
                    "di_gap":        s["di_gap"],
                    "rsi":           s.get("rsi"),
                    "vol_ratio":     s.get("vol_ratio"),
                    "confirmations": s.get("confirmations", ""),
                    "reason":        s["reason"],
                    "best_price":    s["entry"],
                    "be_active":     False,
                    "trail_active":  False,
                }

        # Close open trade at last bar
        if active is not None:
            t          = active
            exit_price = float(df.iloc[-1]["close"])
            raw_pnl    = (exit_price - t["entry"]) if t["signal"] == "BUY" \
                         else (t["entry"] - exit_price)
            commission = (t["entry"] + exit_price) * self.commission_pct
            best_r = (
                (t["best_price"] - t["entry"]) / t["risk"] if t["signal"] == "BUY"
                else (t["entry"] - t["best_price"]) / t["risk"]
            ) if t["risk"] else 0
            trades.append({
                **{k: v for k, v in t.items()
                   if k not in ("current_sl","be_active","trail_active","best_price")},
                "exit_date":   df.index[-1],
                "exit_price":  round(exit_price, 2),
                "exit_reason": "OPEN",
                "max_r_seen":  round(best_r, 2),
                "raw_pnl":     round(raw_pnl, 2),
                "net_pnl":     round(raw_pnl - commission, 2),
                "r_multiple":  round(raw_pnl / t["risk"], 2) if t["risk"] else 0,
                "winner":      (raw_pnl - commission) > 0,
            })

        return trades

    # ─── Stats ───────────────────────────────────────────────────────────────

    def _stats(self, trades: list[dict]) -> dict:
        closed = [t for t in trades if t["exit_reason"] != "OPEN"]
        if not closed:
            return {"total_trades": len(trades), "note": "All open"}

        winners      = [t for t in closed if t["winner"]]
        losers       = [t for t in closed if not t["winner"]]
        total_profit = sum(t["net_pnl"] for t in winners)
        total_loss   = abs(sum(t["net_pnl"] for t in losers))
        net_pnls     = [t["net_pnl"] for t in closed]
        r_mults      = [t["r_multiple"] for t in closed]

        equity   = np.cumsum(net_pnls)
        peak     = np.maximum.accumulate(equity)
        max_dd   = float(np.min(equity - peak))

        exit_counts: dict = {}
        for t in closed:
            exit_counts[t["exit_reason"]] = exit_counts.get(t["exit_reason"], 0) + 1

        monthly: dict = {}
        for t in closed:
            k = str(t["exit_date"])[:7]
            monthly[k] = round(monthly.get(k, 0.0) + t["net_pnl"], 2)

        by_dir: dict = {}
        for t in closed:
            d = t["signal"]
            if d not in by_dir:
                by_dir[d] = {"total": 0, "wins": 0, "pnl": 0.0}
            by_dir[d]["total"] += 1
            by_dir[d]["pnl"]   = round(by_dir[d]["pnl"] + t["net_pnl"], 2)
            if t["winner"]:
                by_dir[d]["wins"] += 1

        # Max R seen before exit (did price ever hit 2R or 3R?)
        max_r_dist = {"0-1R": 0, "1-2R": 0, "2-3R": 0, "3R+": 0}
        for t in closed:
            mr = t.get("max_r_seen", 0)
            if mr < 1:   max_r_dist["0-1R"] += 1
            elif mr < 2: max_r_dist["1-2R"] += 1
            elif mr < 3: max_r_dist["2-3R"] += 1
            else:        max_r_dist["3R+"]  += 1

        return {
            "total_trades":   len(closed),
            "open_trades":    len(trades) - len(closed),
            "winners":        len(winners),
            "losers":         len(losers),
            "win_rate":       round(len(winners) / len(closed) * 100, 1),
            "profit_factor":  round(total_profit / total_loss, 2) if total_loss else float("inf"),
            "total_net_pnl":  round(sum(net_pnls), 2),
            "avg_winner":     round(total_profit / len(winners), 2) if winners else 0,
            "avg_loser":      round(-total_loss / len(losers), 2) if losers else 0,
            "avg_r_multiple": round(float(np.mean(r_mults)), 2),
            "best_trade":     round(max(net_pnls), 2),
            "worst_trade":    round(min(net_pnls), 2),
            "max_drawdown":   round(max_dd, 2),
            "exit_breakdown": exit_counts,
            "max_r_distribution": max_r_dist,
            "by_direction": {
                k: {
                    "total": v["total"],
                    "win_rate": round(v["wins"] / v["total"] * 100, 1),
                    "net_pnl": v["pnl"],
                }
                for k, v in by_dir.items()
            },
            "monthly_pnl": dict(sorted(monthly.items())),
        }

    # ─── Report ──────────────────────────────────────────────────────────────

    def print_report(self, result: dict) -> None:
        symbol = result.get("symbol", "N/A")
        stats  = result.get("stats", {})
        trades = result.get("trades", [])
        W = 82

        print(f"\n{'='*W}")
        print(f"  BACKTEST — {symbol}  |  DI Crossover v4  (ADX < 25, RSI band 45-58/42-55)")
        print(f"  Exit: Hard SL → Breakeven at +1R → Trail(1.5×ATR) at +2R → Target 3R")
        print(f"{'='*W}")

        if not stats or "note" in stats:
            print(f"  Signals: {result.get('signals_generated',0)} | No closed trades."); return

        print(f"  Signals generated : {result['signals_generated']}")
        print(f"  Trades            : {stats['total_trades']} closed  +  {stats['open_trades']} open")
        print(f"  Win Rate          : {stats['win_rate']}%  ({stats['winners']}W / {stats['losers']}L)")
        print(f"  Profit Factor     : {stats['profit_factor']}")
        print(f"  Total Net P&L     : {stats['total_net_pnl']:>10.2f}")
        print(f"  Avg Winner        : {stats['avg_winner']:>10.2f}  (target: >2R avg)")
        print(f"  Avg Loser         : {stats['avg_loser']:>10.2f}  (expected: ~1R)")
        print(f"  Avg R-Multiple    : {stats['avg_r_multiple']:.2f}R  (target: >0.5R)")
        print(f"  Best / Worst      : {stats['best_trade']:.2f} / {stats['worst_trade']:.2f}")
        print(f"  Max Drawdown      : {stats['max_drawdown']:>10.2f}")

        print(f"\n  ── Max Favourable R before exit (did trades get room to run?) ──")
        for bucket, count in stats.get("max_r_distribution", {}).items():
            bar = "█" * count
            print(f"  {bucket:<6}: {count:>3} trades  {bar}")

        print(f"\n  ── Exit Breakdown ──")
        for reason, count in sorted(stats.get("exit_breakdown", {}).items()):
            print(f"  {reason:<15}: {count}")

        print(f"\n  ── By Direction ──")
        for d, data in stats.get("by_direction", {}).items():
            print(f"  {d:<6}: {data['total']} trades | {data['win_rate']}% WR | Net P&L {data['net_pnl']:.2f}")

        print(f"\n  ── Monthly P&L ──")
        vals    = list(stats["monthly_pnl"].values())
        max_abs = max((abs(v) for v in vals), default=1)
        for month, pnl in stats.get("monthly_pnl", {}).items():
            bar  = "█" * max(1, int(abs(pnl) / max_abs * 22))
            sign = "+" if pnl >= 0 else ""
            print(f"  {month}  {sign}{pnl:>9.2f}  {'▲' if pnl>=0 else '▼'} {bar}")

        print(f"\n  ── Trade Log ──")
        print(f"  {'Signal':<11}{'Entry':<11}{'Dir':<5}{'ADX':>5}  "
              f"{'Entry$':>8}  {'Exit$':>8}  {'Reason':<12}"
              f"{'RSI':>5}{'Vol':>5}  {'maxR':>5}  {'P&L':>8}  {'R':>6}")
        print(f"  {'-'*(W-2)}")
        for t in trades[:60]:
            print(
                f"  {str(t['signal_date'])[:10]:<11}"
                f"{str(t['entry_date'])[:10]:<11}"
                f"{t['signal']:<5}{t['adx']:>5.1f}  "
                f"{t['entry']:>8.2f}  {t['exit_price']:>8.2f}  "
                f"{t['exit_reason']:<12}"
                f"{str(t.get('rsi') or '--'):>5}"
                f"{str(t.get('vol_ratio') or '--'):>5}  "
                f"{t.get('max_r_seen', 0):>5.2f}  "
                f"{t['net_pnl']:>8.2f}  {t['r_multiple']:>5.2f}R"
            )
        if len(trades) > 60:
            print(f"  ... and {len(trades)-60} more")
        print(f"{'='*W}\n")


# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    load_dotenv()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    p = argparse.ArgumentParser(description="ADX Backtest v4 — DI Crossover")
    p.add_argument("--symbol",    required=True)
    p.add_argument("--exchange",  default="NSE")
    p.add_argument("--interval",  default="day")
    p.add_argument("--lookback",  type=int,   default=500)
    p.add_argument("--adx-max",   type=float, default=25.0)
    p.add_argument("--no-rsi",    action="store_true")
    p.add_argument("--no-volume", action="store_true")
    p.add_argument("--ema-filter",action="store_true")
    p.add_argument("--save-csv",  action="store_true")
    args = p.parse_args()

    from trading.user_token import fetch_user_token
    kite, user_id = fetch_user_token(log)

    instruments = kite.instruments(args.exchange)
    token = next((i["instrument_token"] for i in instruments
                  if i["tradingsymbol"] == args.symbol), None)
    if not token:
        log.error(f"{args.symbol} not found"); sys.exit(1)

    to_date   = datetime.now()
    from_date = to_date - timedelta(days=args.lookback)
    raw = kite.historical_data(
        instrument_token=token,
        from_date=from_date.strftime("%Y-%m-%d"),
        to_date=to_date.strftime("%Y-%m-%d"),
        interval=args.interval, continuous=False,
    )
    df = pd.DataFrame(raw)
    df.rename(columns={"date": "datetime"}, inplace=True)
    df.set_index("datetime", inplace=True)
    df = df[["open","high","low","close","volume"]]
    log.info(f"Loaded {len(df)} candles for {args.symbol}")

    bt = ADXBacktest(strategy_kwargs={
        "adx_max":       args.adx_max,
        "rsi_filter":    not args.no_rsi,
        "volume_filter": not args.no_volume,
        "ema_filter":    args.ema_filter,
    })
    result = bt.run(df, symbol=args.symbol)
    bt.print_report(result)

    if args.save_csv and result.get("trades"):
        out = f"backtest_{args.symbol}_{datetime.now().strftime('%Y%m%d')}.csv"
        pd.DataFrame(result["trades"]).to_csv(out, index=False)
        log.info(f"Saved → {out}")