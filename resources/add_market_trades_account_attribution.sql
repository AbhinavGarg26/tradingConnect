BEGIN;

ALTER TABLE market_trades
    ADD COLUMN IF NOT EXISTS user_id BIGINT,
    ADD COLUMN IF NOT EXISTS exchange_link_id UUID;

-- Legacy market_trades was a singleton Kite ledger. Backfill only when the
-- database has exactly one active Zerodha link; otherwise ownership is
-- ambiguous and must remain paused for manual classification.
DO $$
DECLARE
    active_link_count INTEGER;
    target_user_id BIGINT;
    target_link_id UUID;
BEGIN
    SELECT COUNT(*)
      INTO active_link_count
      FROM exchange_links
     WHERE provider = 'zerodha' AND is_active = TRUE;

    IF active_link_count = 1 THEN
        SELECT user_id, id
          INTO target_user_id, target_link_id
          FROM exchange_links
         WHERE provider = 'zerodha' AND is_active = TRUE;

        UPDATE market_trades
           SET user_id = target_user_id,
               exchange_link_id = target_link_id
         WHERE user_id IS NULL AND exchange_link_id IS NULL;
    ELSIF EXISTS (
        SELECT 1 FROM market_trades
         WHERE user_id IS NULL OR exchange_link_id IS NULL
    ) THEN
        RAISE NOTICE
            'Legacy market_trades not backfilled: found % active Zerodha links',
            active_link_count;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'fk_market_trades_user'
    ) THEN
        ALTER TABLE market_trades
            ADD CONSTRAINT fk_market_trades_user
            FOREIGN KEY (user_id) REFERENCES users(id) NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'fk_market_trades_exchange_link'
    ) THEN
        ALTER TABLE market_trades
            ADD CONSTRAINT fk_market_trades_exchange_link
            FOREIGN KEY (exchange_link_id) REFERENCES exchange_links(id) NOT VALID;
    END IF;
END $$;

ALTER TABLE market_trades
    VALIDATE CONSTRAINT fk_market_trades_user;

ALTER TABLE market_trades
    VALIDATE CONSTRAINT fk_market_trades_exchange_link;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM market_trades
         WHERE user_id IS NULL OR exchange_link_id IS NULL
    ) THEN
        ALTER TABLE market_trades ALTER COLUMN user_id SET NOT NULL;
        ALTER TABLE market_trades ALTER COLUMN exchange_link_id SET NOT NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_market_trades_user_entry_time
    ON market_trades (user_id, entry_time DESC);

CREATE INDEX IF NOT EXISTS ix_market_trades_exchange_link_entry_time
    ON market_trades (exchange_link_id, entry_time DESC);

COMMIT;

-- Verification: live buying must remain paused if this returns any rows.
SELECT COUNT(*) AS unattributed_market_trades
  FROM market_trades
 WHERE user_id IS NULL OR exchange_link_id IS NULL;
