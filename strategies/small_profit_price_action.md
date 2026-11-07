# Small-profit NIFTY price action (paper first)

This strategy buys the nearest-expiry ATM CE after an upside opening-range
breakout, retest, and bullish follow-through; it buys the ATM PE for the
mirrored downside setup. It uses completed five-minute candles only and stops
looking for entries after 14:30. Defaults are an 8-point NIFTY invalidation,
12-point target, no more than three filled entries, a ₹2,500 daily loss cap,
and a two-loss circuit breaker.

₹5,000 is the daily **stop-trading profit threshold**, not a promised daily
income. The runner must pass actual net option P&L to `record_closed_trade`
after every confirmed exit. Once the threshold is reached, no further entries
are allowed that day. Position size must be calculated from the configured
rupee risk and the broker-held option stop; it must never be increased to make
the ₹5,000 target easier to reach.

Run in paper mode and backtest with realistic option premiums, spread,
slippage, brokerage, taxes, and expiry-day behavior before considering live
use. Live integration must use `trading.strategy_risk.check_new_entry`, the
existing broker stop-limit protection, persisted trade journal, and guardian.
