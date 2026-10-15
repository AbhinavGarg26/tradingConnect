"""Live adapter for the same NIFTY 15-minute decision engine as the backtest.

Register ``adapter.on_candle_close`` with WebSocketEngine after arranging a
15-minute NIFTY index subscription. ``execute`` receives each decision and
must confirm broker fills, persist positions, and handle restart recovery.
"""

from strategies.nifty_15m_breakout import Nifty15mLiveAdapter
