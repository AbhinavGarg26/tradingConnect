"""Experimental V3C: CE signals require a green completed candle; PE unchanged."""
from strategies.nifty_30m_reference import Nifty30mReference

STRATEGY_NAME = 'nifty_30m_reference_ce_green'
STRATEGY_VERSION = 'v3c'


class Nifty30mReferenceCEGreen(Nifty30mReference):
    def _entry_allowed(self, side, bar):
        # Equal open/close is not green. Never inspect the next fill candle's close.
        return side != 'CE' or bar.close > bar.open
