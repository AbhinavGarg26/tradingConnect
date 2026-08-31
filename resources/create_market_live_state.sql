BEGIN;

CREATE TABLE IF NOT EXISTS market_live_state (
    id BIGSERIAL PRIMARY KEY,
    entity_type VARCHAR(32) NOT NULL,
    entity_key VARCHAR(150) NOT NULL,
    metric_type VARCHAR(32) NOT NULL,
    metric_key VARCHAR(180) NOT NULL,
    timeframe_minutes SMALLINT,
    numeric_value NUMERIC(20, 6),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    event_time TIMESTAMPTZ,
    is_complete BOOLEAN,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_market_live_state_metric
        UNIQUE (entity_type, entity_key, metric_type, metric_key)
);

CREATE INDEX IF NOT EXISTS ix_market_live_state_lookup
    ON market_live_state (entity_type, entity_key, metric_type, updated_at DESC);

CREATE INDEX IF NOT EXISTS ix_market_live_state_candles
    ON market_live_state (entity_key, timeframe_minutes, event_time DESC)
    WHERE metric_type = 'CANDLE';

COMMIT;
