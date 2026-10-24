"""Bearish-only 15-minute EMA comparison using the common audited signal engine."""
from backtest.nifty_30m_put_comparison import main

if __name__=='__main__':
    main(default_timeframe=15,default_output_dir='outputs/nifty_15m_put_comparison')
