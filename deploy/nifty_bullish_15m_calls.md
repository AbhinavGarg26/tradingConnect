# NIFTY 15m EMA 9/21 · Calls

Strategy key: `nifty_bullish_15m_9_21`; version: `bullish_15m_9_21_v1`.

A completed NIFTY 15-minute EMA9 cross above EMA21 enters one ATM monthly CE lot, subject to the ₹30,000 premium cap. Cross below exits. Signals use continuous session EMA history, including prior sessions, and only become eligible after the candle closes. Stay flat until a fresh bullish crossover; no entry merely because EMA9 is above EMA21. Final-session signals are eligible at the next session opening. Existing intraday freshness checks apply.

Monthly selection, rollover two trading sessions before expiry, overnight holding and confirmed-fill recovery reuse the existing EMA executor. No fixed stop or target. Daily 20/50 and bearish 15m 9/21 retain their own settings, positions and caches.

The strategy is registered in the local performance list and operations controls. The new `nifty-bullish-15m-ema.service` and timer are templates only and default to paper. No service has been installed, started or enabled; no activation request has been created. Deployment of both repositories is required before the server UI reflects these changes. Any broker-connected activation must be performed by the account owner.

Historical research: `outputs/nifty_ce_15m_five_pairs/` and `outputs/nifty_bullish_ema_timeframes/`. Those reports measure NIFTY index points, not monthly call returns.
