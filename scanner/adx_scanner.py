"""
scanner/adx_scanner.py
======================
Scans instrument list for fresh DI crossover setups (ADX < 29).

Freshness check: signal must be on the last or second-to-last bar.
Older signals are not actionable.

Usage:
    python scanner/adx_scanner.py --interval day
    python scanner/adx_scanner.py --interval 15minute --no-volume
"""

import logging
import time
import argparse
from datetime import datetime, timedelta

import pandas as pd
from kiteconnect import KiteConnect

from strategies.adx_crossover import ADXCrossoverStrategy, apply_adx_crossover_strategy

log = logging.getLogger(__name__)
RATE_DELAY = 0.35


def scan_market_for_candidates(kite, instrument_tokens: list, interval: str = "day") -> list:
    """
    Scans a targeted watch list to uncover immediate entry setups.
    """
    candidates = []

    to_date = datetime.now()
    from_date = to_date - timedelta(days=100)  # Fetch ample room for accurate indicators

    for token in instrument_tokens:
        try:
            # Fetch historical data from Kite API
            records = kite.historical_data(
                instrument_token=token,
                from_date=from_date.strftime("%Y-%m-%d"),
                to_date=to_date.strftime("%Y-%m-%d"),
                interval=interval
            )

            if not records:
                continue

            df = pd.DataFrame(records)
            df_with_signals = apply_adx_crossover_strategy(df)

            # Extract the last finalized candle status
            latest_row = df_with_signals.iloc[-1]

            if latest_row['signal'] == 1:
                candidates.append({"token": token, "direction": "BULLISH_BREAKOUT"})
            elif latest_row['signal'] == -1:
                candidates.append({"token": token, "direction": "BEARISH_BREAKOUT"})

        except Exception as e:
            print(f"Skipping Token {token} due to an execution hurdle: {e}")

    return candidates

class ADXScanner:
    def __init__(self, kite: KiteConnect, interval="day", lookback=120,
                 adx_max=29.0, rsi_filter=True, volume_filter=True, ema_filter=False):
        self.kite     = kite
        self.interval = interval
        self.lookback = lookback
        self.strategy = ADXCrossoverStrategy(
            adx_max=adx_max, rsi_filter=rsi_filter,
            volume_filter=volume_filter, ema_filter=ema_filter,
        )

    def fetch(self, token, symbol):
        to_date   = datetime.now()
        from_date = to_date - timedelta(days=self.lookback * 2)
        try:
            data = self.kite.historical_data(
                instrument_token=token,
                from_date=from_date.strftime("%Y-%m-%d"),
                to_date=to_date.strftime("%Y-%m-%d"),
                interval=self.interval, continuous=False,
            )
        except Exception as e:
            log.warning(f"[{symbol}] {e}"); return None
        if not data: return None
        df = pd.DataFrame(data)
        df.rename(columns={"date": "datetime"}, inplace=True)
        df.set_index("datetime", inplace=True)
        return df[["open","high","low","close","volume"]].tail(self.lookback)

    def scan_one(self, token, symbol):
        df = self.fetch(token, symbol)
        if df is None or len(df) < 40: return None
        signals = self.strategy.run(df)
        if not signals: return None
        latest   = signals[-1]
        bar_list = list(df.index)
        try:
            pos = bar_list.index(latest["signal_date"])
            if (len(bar_list) - 1 - pos) > 2: return None   # stale
        except ValueError:
            pass
        latest.update({"symbol": symbol, "instrument_token": token})
        return latest

    def scan(self, instruments):
        candidates = []
        for idx, inst in enumerate(instruments, 1):
            token  = inst["instrument_token"]
            symbol = inst.get("tradingsymbol", str(token))
            log.info(f"[{idx}/{len(instruments)}] {symbol}")
            result = self.scan_one(token, symbol)
            if result:
                candidates.append(result)
                log.info(f"  ✓ {result['signal']} | ADX={result['adx']:.1f} | "
                         f"+DI {result['prev_plus_di']:.1f}→{result['plus_di']:.1f} | "
                         f"-DI {result['prev_minus_di']:.1f}→{result['minus_di']:.1f}")
            time.sleep(RATE_DELAY)
        return candidates


    def print_candidates(self, candidates):
        if not candidates:
            print("\nNo fresh DI crossover setups found."); return
        print(f"\n{'='*78}")
        print(f"  DI CROSSOVER SCANNER  ─  {len(candidates)} fresh candidates  (ADX < 29)")
        print(f"{'='*78}")
        for label in ("BUY", "SELL"):
            subset = [c for c in candidates if c["signal"] == label]
            if not subset: continue
            print(f"\n  ── {label} ──  (+DI crossed {'above' if label=='BUY' else 'below'} -DI while ADX ranging)")
            print(f"  {'Symbol':<18} {'ADX':>5} {'prev+DI':>8} {'cur+DI':>8} "
                  f"{'prev-DI':>8} {'cur-DI':>8} {'RSI':>5} {'Vol':>5} "
                  f"{'Entry':>9} {'SL':>9} {'Tgt':>9}")
            print(f"  {'-'*76}")
            for c in sorted(subset, key=lambda x: abs(x["di_gap"]), reverse=True):
                print(
                    f"  {c['symbol']:<18} {c['adx']:>5.1f} "
                    f"{c['prev_plus_di']:>8.1f} {c['plus_di']:>8.1f} "
                    f"{c['prev_minus_di']:>8.1f} {c['minus_di']:>8.1f} "
                    f"{str(c.get('rsi') or '--'):>5} {str(c.get('vol_ratio') or '--'):>5} "
                    f"{c['entry']:>9.2f} {c['sl']:>9.2f} {c['target']:>9.2f}"
                )
        print(f"{'='*78}\n")


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    p = argparse.ArgumentParser()
    p.add_argument("--interval",        default="day")
    p.add_argument("--lookback",        type=int,   default=120)
    p.add_argument("--adx-max",         type=float, default=29.0)
    p.add_argument("--no-rsi",          action="store_true")
    p.add_argument("--no-volume",       action="store_true")
    p.add_argument("--ema-filter",      action="store_true")
    p.add_argument("--instruments-csv", default="resources/instrument_list.csv")
    p.add_argument("--save-csv",        action="store_true")
    args = p.parse_args()

    from trading.user_token import fetch_user_token
    kite, user_id = fetch_user_token(log)

    try:
        instruments = pd.read_csv(args.instruments_csv)[["instrument_token","tradingsymbol"]].to_dict("records")
    except FileNotFoundError:
        log.error(f"Not found: {args.instruments_csv}"); sys.exit(1)

    scanner    = ADXScanner(kite, args.interval, args.lookback, args.adx_max,
                            not args.no_rsi, not args.no_volume, args.ema_filter)
    candidates = scanner.scan(instruments)
    scanner.print_candidates(candidates)

    if args.save_csv and candidates:
        out = f"di_crossover_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
        pd.DataFrame(candidates).to_csv(out, index=False)
        log.info(f"Saved → {out}")