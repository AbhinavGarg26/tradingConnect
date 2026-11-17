-- Run before enabling reversal_enabled. No broker orders are placed by this migration.
CREATE TABLE IF NOT EXISTS market_reversal_states (
    user_id bigint NOT NULL REFERENCES users(id),
    trading_date date NOT NULL,
    state jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(user_id, trading_date)
);
INSERT INTO market_configs(id,user_id,key,value,data_type,category,created_at,updated_at)
SELECT gen_random_uuid(), u.id, c.key, c.value, c.type, 'reversal', now(), now()
FROM users u CROSS JOIN (VALUES
 ('reversal_enabled','false','boolean'),
 ('reversal_drawdown_pct','5','float'),
 ('reversal_max_peak_profit_pct','10','float'),
 ('reversal_max_per_day','1','integer'),
 ('reversal_cooldown_seconds','300','integer'),
 ('reversal_confirmation_window_seconds','180','integer'),
 ('reversal_funds_buffer_pct','5','float'),
 ('reversal_daily_loss_limit','2000','float'),
 ('reversal_entry_cutoff','15:00','string')
) c(key,value,type)
ON CONFLICT(user_id,key) DO NOTHING;
