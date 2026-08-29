# system_configs reference
# Insert these rows for your user after running migrations.
# Rails config panel writes these; Python engine reads them via MarketConfigRepo.
#
# key                  | default  | type    | category      | description
# ---------------------|----------|---------|---------------|----------------------------------
# trail_trigger_tf     | 1        | integer | sl_strategy   | Candle timeframe (minutes) that triggers trailing SL evaluation. 1 = 1-min candle close.
# atr_period           | 14       | integer | sl_strategy   | ATR lookback period
# atr_multiplier       | 1.5      | float   | sl_strategy   | ATR multiplier for SL distance
# trail_method         | swing    | string  | sl_strategy   | Default trail method: swing | atr | r_multiple | ema
# risk_per_trade       | 2000     | integer | risk          | Max risk per trade in INR
# max_open_trades      | 5        | integer | risk          | Max concurrent open trades
# support_zone_buffer  | 0.3      | float   | sl_strategy   | % buffer around support levels for rejection detection
# telegram_bot_token   |          | string  | alert         | Telegram bot token from @BotFather
# telegram_chat_id     |          | string  | alert         | Telegram chat ID to send alerts to

# SQL to seed defaults for a user:
#
--INSERT INTO market_configs (id, user_id, key, value, data_type, category, created_at, updated_at) VALUES
--   (gen_random_uuid(), '975447485', 'trail_trigger_tf',    '1',     'integer', 'sl_strategy', now(), now()),
--   (gen_random_uuid(), '975447485', 'atr_period',          '14',    'integer', 'sl_strategy', now(), now()),
--   (gen_random_uuid(), '975447485', 'atr_multiplier',      '1.5',   'float',   'sl_strategy', now(), now()),
--   (gen_random_uuid(), '975447485', 'trail_method',        'swing', 'string',  'sl_strategy', now(), now()),
--   (gen_random_uuid(), '975447485', 'risk_per_trade',      '2000',  'integer', 'risk', now(), now()),
--   (gen_random_uuid(), '975447485', 'max_open_trades',     '5',     'integer', 'risk', now(), now()),
--   (gen_random_uuid(), '975447485', 'support_zone_buffer', '0.3',   'float',   'sl_strategy', now(), now()),
--   (gen_random_uuid(), '975447485', 'telegram_bot_token',  '',      'string',  'alert', now(), now()),
--   (gen_random_uuid(), '975447485', 'telegram_chat_id',    '',      'string',  'alert', now(), now());


--
---- Add these alongside the existing ones
--INSERT INTO market_configs (id, user_id, key, value, data_type, category, created_at, updated_at) VALUES
-- (gen_random_uuid(), '975447485', 'ema_trail_period', '21', 'integer', 'sl_strategy', now(), now()),
-- (gen_random_uuid(), '975447485', 'swing_lookback',   '5',  'integer', 'sl_strategy', now(), now());
--```