# Local Setup Guide — Trading System

Complete step-by-step guide to run the Python engine and Rails dashboard locally.

---

## Prerequisites

Make sure these are installed before starting:

| Tool | Version | Check |
|---|---|---|
| PostgreSQL | 14+ | `psql --version` |
| Python | 3.11+ | `python --version` |
| Ruby | 3.2+ | `ruby --version` |
| Rails | 7.1+ | `rails --version` |
| Node.js | 18+ | `node --version` |
| Git | any | `git --version` |

---

## 1. Project structure

Create the following folder layout:

```
trading-system/
├── python-engine/       ← Python trading engine
│   └── trading/
└── rails-dashboard/     ← Rails UI
```

```bash
mkdir trading-system
cd trading-system
mkdir python-engine rails-dashboard
```

---

## 2. PostgreSQL setup

```bash
# Start PostgreSQL (macOS with Homebrew)
brew services start postgresql@14

# Linux
sudo systemctl start postgresql

# Create database and user
psql postgres -c "CREATE USER trading_user WITH PASSWORD 'trading_pass';"
psql postgres -c "CREATE DATABASE trading_db OWNER trading_user;"
psql postgres -c "GRANT ALL PRIVILEGES ON DATABASE trading_db TO trading_user;"

# Verify connection
psql -U trading_user -d trading_db -c "SELECT version();"
```

---

## 3. Python engine setup

### 3a. Create virtual environment

```bash
cd trading-system/python-engine

python -m venv venv
source venv/bin/activate          # macOS/Linux
# venv\Scripts\activate           # Windows

pip install --upgrade pip
pip install -r requirements.txt
```

### 3b. Create .env file

```bash
cat > .env << 'EOF'
# Database
DATABASE_URL=postgresql+psycopg2://trading_user:trading_pass@localhost:5432/trading_db

# Encryption key — generate once and keep safe
# Run: python -c "import secrets; print(secrets.token_hex(32))"
DB_ENCRYPTION_KEY=REPLACE_WITH_YOUR_64_CHAR_HEX_KEY

# Optional: print SQL queries for debugging
SQL_ECHO=false
EOF
```

Generate your encryption key:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
# Copy the output and paste into .env as DB_ENCRYPTION_KEY
```

### 3c. Run Alembic migrations

```bash
# Initialise Alembic (only first time)
alembic init alembic

# Edit alembic.ini — set sqlalchemy.url to your DATABASE_URL
# Or set it dynamically in alembic/env.py:
#   config.set_main_option("sqlalchemy.url", os.environ["DATABASE_URL"])

# Run migrations
alembic upgrade head

# Verify tables were created
psql -U trading_user -d trading_db -c "\dt"
```

You should see: `users`, `instruments`, `exchange_links`, `trades`, `positions`,
`stop_loss_histories`, `order_events`, `support_levels`, `system_configs`, `session_logs`

### 3d. Seed initial data

```bash
# Open Python shell
python

>>> from dotenv import load_dotenv
>>> load_dotenv()
>>> from trading.database import get_db
>>> from trading.models import User
>>> from trading.repositories import MarketConfigRepo

# Create your user
>>> with get_db() as db:
...     user = User(name="Your Name", email="you@example.com")
...     db.add(user)
...     db.flush()
...     MarketConfigRepo.seed_defaults(db, user.id)
...     print(f"User created: {user.id}")

# Note the user ID printed — you'll need it for exchange_link setup
```

### 3e. Add Kite credentials

```bash
python

>>> from dotenv import load_dotenv
>>> load_dotenv()
>>> import uuid
>>> from trading.database import get_db
>>> from trading.exchange_link import ExchangeLinkRepo

>>> USER_ID = uuid.UUID("PASTE_YOUR_USER_ID_HERE")

>>> with get_db() as db:
...     link = ExchangeLinkRepo.create(
...         db,
...         user_id=USER_ID,
...         access_id="YOUR_KITE_API_KEY",
...         access_secret="YOUR_KITE_API_SECRET",
...         provider="zerodha",
...         account_ref="YOUR_ZERODHA_USER_ID",  # e.g. AB1234
...     )
...     print(f"Exchange link created: {link.id}")
```

### 3f. Seed system configs with Telegram details

```bash
python

>>> from dotenv import load_dotenv
>>> load_dotenv()
>>> import uuid
>>> from trading.database import get_db
>>> from trading.models import MarketConfig

>>> USER_ID = uuid.UUID("PASTE_YOUR_USER_ID_HERE")

>>> with get_db() as db:
...     # Update telegram config (already seeded as empty strings)
...     for key, value in [
...         ("telegram_bot_token", "YOUR_BOT_TOKEN"),
...         ("telegram_chat_id",   "YOUR_CHAT_ID"),
...     ]:
...         cfg = db.query(MarketConfig).filter_by(user_id=USER_ID, key=key).first()
...         if cfg:
...             cfg.value = value
...     print("Telegram config updated")
```

### 3g. Add instruments

```bash
# Seed a few instruments manually for testing
python

>>> from dotenv import load_dotenv
>>> load_dotenv()
>>> from trading.database import get_db
>>> from trading.models import Instrument

>>> with get_db() as db:
...     instruments = [
...         Instrument(
...             symbol="NIFTY",
...             exchange="NSE",
...             segment="EQ",
...             instrument_type="EQ",
...             instrument_token=256265,    # Kite token for NIFTY 50
...             lot_size=1,
...             tick_size=0.05,
...             is_active=True,
...         ),
...         Instrument(
...             symbol="RELIANCE",
...             exchange="NSE",
...             segment="EQ",
...             instrument_type="EQ",
...             instrument_token=738561,
...             lot_size=1,
...             tick_size=0.05,
...             is_active=True,
...         ),
...     ]
...     db.add_all(instruments)
...     print(f"Added {len(instruments)} instruments")

# For F&O instruments, get the correct instrument_token from:
# kite.instruments("NFO")  — after you have a valid session
```

### 3h. Verify Python setup

```bash
python -c "
from dotenv import load_dotenv
load_dotenv()
from trading.database import get_db
from trading.repositories import UserRepo

with get_db() as db:
    user = UserRepo.get_active(db)
    print(f'User: {user.email}')
    print(f'MarketConfigs: {len(user.system_configs)}')
    print(f'Exchange link: {user.exchange_link}')
    print('Python setup OK')
"
```

---

## 4. Rails dashboard setup

### 4a. Setup Rails app

```bash
cd trading-system/rails-dashboard

# If starting fresh:
rails new . --database=postgresql --skip-test --skip-action-mailer

# Install gems
bundle install

# Add to Gemfile if not present:
# gem "kaminari"
# gem "business_time"
# gem "faraday"
# gem "faraday-json"
bundle install
```

### 4b. Configure database

```bash
# config/database.yml
cat > config/database.yml << 'EOF'
default: &default
  adapter: postgresql
  encoding: unicode
  host: localhost
  username: trading_user
  password: trading_pass
  pool: <%= ENV.fetch("RAILS_MAX_THREADS") { 5 } %>

development:
  <<: *default
  database: trading_db   # same DB as Python engine

production:
  <<: *default
  database: trading_db
  url: <%= ENV["DATABASE_URL"] %>
EOF
```

### 4c. Create Rails credentials / environment

```bash
# Create .env or use Rails credentials
cat > .env << 'EOF'
DB_ENCRYPTION_KEY=SAME_KEY_AS_PYTHON_ENGINE
RAILS_ENV=development
EOF

# Add dotenv-rails to Gemfile:
# gem "dotenv-rails", groups: [:development, :test]
bundle install
```

### 4d. Copy all generated files into Rails app

Place the files you downloaded into the correct locations:

```
rails-dashboard/
├── app/
│   ├── controllers/
│   │   ├── application_controller.rb     ← existing
│   │   ├── dashboard_controller.rb       ← from outputs
│   │   ├── trades_controller.rb          ← from outputs
│   │   ├── support_levels_controller.rb  ← split from combined file
│   │   ├── instruments_controller.rb     ← split from combined file
│   │   ├── system_configs_controller.rb  ← split from combined file
│   │   └── trading_sessions_controller.rb ← from outputs
│   ├── models/
│   │   ├── trade.rb                      ← from outputs
│   │   ├── instrument.rb                 ← create (see below)
│   │   ├── support_level.rb              ← create (see below)
│   │   ├── system_config.rb              ← create (see below)
│   │   ├── exchange_link.rb              ← from outputs
│   │   ├── session_log.rb                ← from outputs
│   │   └── user.rb                       ← create (see below)
│   └── views/
│       ├── layouts/application.html.erb  ← from outputs
│       ├── dashboard/index.html.erb      ← from outputs
│       ├── trades/show.html.erb          ← from outputs
│       ├── support_levels/index.html.erb ← from outputs
│       └── system_configs/index.html.erb ← from outputs
└── config/routes.rb                      ← from routes_final.rb
```

### 4e. Create minimal Rails models (mirrors Python models)

```ruby
# app/models/user.rb
class User < ApplicationRecord
  has_one  :exchange_link
  has_many :trades
  has_many :system_configs
  has_many :support_levels
  has_many :session_logs
  scope :active, -> { where(is_active: true) }
end

# app/models/instrument.rb
class Instrument < ApplicationRecord
  has_many :trades
  has_many :support_levels
  scope :active, -> { where(is_active: true) }

  def display_name
    return "#{symbol} #{strike_price} #{instrument_type} #{expiry_date&.strftime('%d%b%y')&.upcase}" if instrument_type.in?(%w[CE PE])
    return "#{symbol} FUT #{expiry_date&.strftime('%d%b%y')&.upcase}" if instrument_type == "FUT"
    symbol
  end
end

# app/models/support_level.rb
class SupportLevel < ApplicationRecord
  belongs_to :instrument
  belongs_to :user
  scope :active, -> { where(is_active: true) }
end

# app/models/system_config.rb
class MarketConfig < ApplicationRecord
  self.table_name = "system_configs"
  belongs_to :user

  def typed_value
    case data_type
    when "integer" then value.to_i
    when "float"   then value.to_f
    when "boolean" then value == "true"
    else value
    end
  end
end

# app/models/stop_loss_history.rb
class StopLossHistory < ApplicationRecord
  self.table_name = "stop_loss_histories"
  belongs_to :trade
end

# app/models/order_event.rb
class OrderEvent < ApplicationRecord
  belongs_to :trade
end

# app/models/position.rb
class Position < ApplicationRecord
  belongs_to :trade
  belongs_to :instrument
end
```

### 4f. Run Rails (does NOT run migrations — schema already exists)

```bash
# Rails reads the existing PostgreSQL schema — do NOT run rails db:migrate
# The schema was already created by Alembic

# However, tell Rails about the schema:
rails db:schema:dump   # dumps current DB state to schema.rb

# Start Rails
rails server -p 3000
```

### 4g. Verify Rails setup

Open `http://localhost:3000` — you should see the dashboard.

Check these pages load:
- `http://localhost:3000/` — dashboard with stats
- `http://localhost:3000/trading_session/new` — token refresh UI
- `http://localhost:3000/support_levels` — support levels
- `http://localhost:3000/system_configs` — config editor

---

## 5. Daily startup sequence

Every morning (after 6 AM IST when Kite tokens expire):

### Step 1 — Start Rails (always running)

```bash
cd rails-dashboard
rails server -p 3000
```

### Step 2 — Start Python engine

```bash
cd python-engine
source venv/bin/activate
python -m trading.main
```

The engine will:
1. Check session token
2. If expired → send Telegram alert with Kite login URL
3. Wait up to 30 minutes for you to paste token

### Step 3 — Refresh Kite token

Open the Telegram alert → click the Kite login link → login → copy `request_token` from the redirect URL → paste at `http://localhost:3000/trading_session/new` → click Activate.

The engine detects the fresh token and starts automatically.

---

## 6. Verify the full flow end to end

```bash
# 1. Check Python engine logs
tail -f python-engine/trading_engine.log

# 2. Check DB for live data
psql -U trading_user -d trading_db -c "SELECT count(*) FROM trades WHERE status='open';"
psql -U trading_user -d trading_db -c "SELECT * FROM system_configs LIMIT 5;"
psql -U trading_user -d trading_db -c "SELECT * FROM exchange_links;"

# 3. Check WebSocket is receiving ticks (look for these log lines)
# WebSocketEngine: connected
# WebSocketEngine: subscribed N tokens (batch 1)
# Candle closed — token=XXXXX tf=1min O=... H=... L=... C=...
```

---

## 7. Common issues and fixes

### Issue: `DB_ENCRYPTION_KEY` not set
```
RuntimeError: DB_ENCRYPTION_KEY environment variable is not set
```
**Fix:** Make sure `.env` is in the root of `python-engine/` and `load_dotenv()` is called before any imports from `trading/`.

---

### Issue: Alembic can't find migration
```
alembic.util.exc.CommandError: Can't locate revision identified by '001_trading_system'
```
**Fix:** Check `alembic/versions/` contains `001_trading_system.py` and `alembic.ini` points to the correct DB URL.

---

### Issue: Rails can't connect to DB
```
PG::ConnectionBad: FATAL: password authentication failed
```
**Fix:** Verify `config/database.yml` credentials match what you created in step 2.

---

### Issue: Engine starts but no ticks
```
WebSocketEngine: subscribed 0 tokens
```
**Fix:** No active instruments in DB. Run the instrument seeding step (3g) or set `is_active=True` on existing instruments.

---

### Issue: `pgp_sym_decrypt` error in Rails
```
ActiveRecord::StatementInvalid: pgp_sym_decrypt: Wrong key or corrupt data
```
**Fix:** `DB_ENCRYPTION_KEY` in Rails `.env` doesn't match the key used in Python when credentials were stored. They must be identical.

---

### Issue: `kaminari` not found for trade history pagination
```
NoMethodError: undefined method 'page' for Trade
```
**Fix:** Add `gem "kaminari"` to Gemfile and run `bundle install`.

---

## 8. Useful commands cheat sheet

```bash
# Python engine
python -m trading.main                          # start engine
python -c "from trading.database import engine; print(engine.url)"  # verify DB connection

# PostgreSQL
psql -U trading_user -d trading_db             # open DB shell
\dt                                            # list tables
\d trades                                      # describe table
SELECT * FROM stop_loss_histories ORDER BY adjusted_at DESC LIMIT 10;
SELECT * FROM order_events WHERE status='REJECTED';

# Rails
rails server -p 3000                           # start dashboard
rails console                                  # Rails console
Trade.open_trades.count                        # count open trades
Trade.dashboard_summary                        # run summary query

# Logs
tail -f python-engine/trading_engine.log       # Python engine logs
tail -f rails-dashboard/log/development.log    # Rails logs
```