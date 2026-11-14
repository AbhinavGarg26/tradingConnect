# One Direction Profit Lock V2.1 (paper only)

This overlay addresses the behavior observed in the November 11–13, 2026
trade journal. It is not a standalone source of entries and it does not claim
that trend indicators predict a profitable trade.

Rules:

- Use completed candles only.
- CE is permitted only when price is above VWAP, fast EMA is above slow EMA,
  ADX is at least 25, and +DI leads -DI by at least 8.
- PE uses the exact mirrored conditions.
- The first accepted entry locks the session to that direction. No CE/PE
  reversal is allowed during the session.
- Only one position may be open at a time.
- Maximum three entries, maximum two consecutive losses, ₹3,000 maximum daily
  loss, no new entries at or after 12:00 IST.
- Once closed net P&L reaches ₹2,000, stop taking new entries for the day.
- Risk-size every entry so its planned broker-stop loss is at most ₹1,000.
- Once realized plus unrealized session P&L reaches ₹5,000, retain at least
  70% of the highest observed session P&L. Exit and lock the day on a breach.
- For a long option, once its observed peak reaches +10%, move the broker stop
  to a price that locks +5%. Thereafter, lock half of each additional peak
  gain. Never move the stop backward.
- A reversal needs two consecutive completed trend candles, a 15-minute
  cooldown after exit, and is allowed only once per session.

The runner must use net P&L after all charges and persist session direction,
entry count, P&L, and lock reason. A process restart must fail closed until
that state is recovered. Test with historical option premiums, spread,
slippage, brokerage, taxes, and realistic stop fills before live activation.
