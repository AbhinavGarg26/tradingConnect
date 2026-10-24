"""Same five CE EMA crossover pairs on completed 30-minute candles."""
import argparse
from backtest.nifty_ce_15m_comparison import compare

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',default='outputs/nifty_ce_30m_five_pairs')
    args=parser.parse_args()
    compare(args.output_dir,timeframe='30m')
