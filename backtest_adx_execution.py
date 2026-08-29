import os
import logging
import datetime
import pandas as pd
from dotenv import load_dotenv

from backtest.adx_backtest import run_detailed_backtest, calculate_performance_metrics
from data.data_manager import get_data
from scanner_divergence import WATCHLIST
from strategies.adx_crossover import apply_adx_crossover_strategy, apply_advanced_strategy

load_dotenv()
# User Custom Modules
from trading.user_token import fetch_user_token

# Setup Logger
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


def main():
    kite, user_id = fetch_user_token(log)

    all_portfolio_trades = []

    for asset in WATCHLIST:
        symbol = asset["tradingsymbol"]
        token = asset["instrument_token"]

        try:
            # --- USE CACHE MANAGER HERE ---
            df = get_data(kite, symbol, token)

            if df.empty:
                continue

            df_with_signals = apply_adx_crossover_strategy(df, adx_threshold=28.0)
            # df_with_signals = apply_advanced_strategy(df, 2.5)
            summary, trade_logs = run_detailed_backtest(df_with_signals)

            print(f"\n📈 ASSET METRICS: {symbol}")
            print(f"  -> Total Executed Cycles: {summary['Total Trades']}")
            print(f"  -> Net Return: {summary['Net Return (%)']}% | Expectancy: {summary['Expectancy per Trade (%)']}%")

            if not trade_logs.empty:
                trade_logs.insert(0, 'Symbol', symbol)
                all_portfolio_trades.append(trade_logs)

                trade_logs['Entry Time'] = pd.to_datetime(trade_logs['Entry Time']).dt.strftime('%Y-%m-%d')
                trade_logs['Exit Time'] = pd.to_datetime(trade_logs['Exit Time']).dt.strftime('%Y-%m-%d')

                # --- UPDATED: Added "Reason" to the terminal output ---
                printed_cols = [
                    "Type", "Reason", "Entry Time", "Entry Price",
                    "Exit Time", "Exit Price", "PnL (%)"
                ]
                print(trade_logs[printed_cols].to_string(index=False))
            else:
                print("  -> 📝 No structured trades matched entry profiles inside the timeline.")
            print("-" * 80)

        except Exception as err:
            log.error(f"Failed to complete backtest sequence for {symbol}: {err}")

    # =====================================================================
    # FINAL PORTFOLIO CALCULATION
    # =====================================================================
    print("\n" + "=" * 80)
    print("🌍 FINAL SYSTEM METRICS (ENTIRE PORTFOLIO)")
    print("=" * 80)

    if all_portfolio_trades:
        # Combine all individual asset trade logs into one massive dataframe
        master_trade_df = pd.concat(all_portfolio_trades, ignore_index=True)

        # Calculate the metrics on the master dataframe
        portfolio_metrics = calculate_performance_metrics(master_trade_df)

        for key, value in portfolio_metrics.items():
            # Add a % sign to formatting for percentage values
            suffix = "%" if "(%)" in key and value != "INF" else ""
            print(f"  -> {key}: {value}{suffix}")

    else:
        print("  -> No trades executed across the entire portfolio during this period.")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    main()