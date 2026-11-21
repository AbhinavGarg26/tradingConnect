-- Apply to the shared PostgreSQL database before restarting kite-market-fetcher.
CREATE TABLE IF NOT EXISTS nifty_option_snapshots (
    session_date date NOT NULL,
    expiry_role text NOT NULL CHECK (expiry_role IN ('nearest_weekly','next_weekly','monthly')),
    snapshot_at timestamptz NOT NULL,
    nifty_spot numeric(14,4) NOT NULL,
    atm_strike numeric(14,4) NOT NULL,
    instrument_token bigint NOT NULL,
    symbol text NOT NULL,
    expiry date NOT NULL,
    strike numeric(14,4) NOT NULL,
    option_type text NOT NULL CHECK (option_type IN ('CE','PE')),
    lot_size integer NOT NULL,
    tick_size numeric(12,4) NOT NULL,
    PRIMARY KEY (session_date, expiry_role, instrument_token)
);
CREATE INDEX IF NOT EXISTS nifty_option_snapshots_token_day
    ON nifty_option_snapshots (instrument_token, session_date);

CREATE TABLE IF NOT EXISTS nifty_option_candles (
    session_date date NOT NULL,
    instrument_token bigint NOT NULL,
    candle_at timestamptz NOT NULL,
    open numeric(14,4) NOT NULL,
    high numeric(14,4) NOT NULL,
    low numeric(14,4) NOT NULL,
    close numeric(14,4) NOT NULL,
    volume bigint,
    open_interest bigint,
    PRIMARY KEY (instrument_token, candle_at)
);
CREATE INDEX IF NOT EXISTS nifty_option_candles_day
    ON nifty_option_candles (session_date, instrument_token, candle_at);
