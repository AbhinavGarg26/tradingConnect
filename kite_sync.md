cd /Users/abhinavgarg/Documents/Projects/kiteConnect
source venv/bin/activate

# Dry run first
python kite_csv_import.py --file ~/Downloads/tradebook-UTE930-FO.csv --dry-run

# Import all
python kite_csv_import.py --file ~/Downloads/tradebook-UTE930-FO.csv

# Import specific month only
python kite_csv_import.py --file ~/Downloads/tradebook-UTE930-FO.csv --from 2026-03-01 --to 2026-03-22

###################

cd /Users/abhinavgarg/Documents/Projects/kiteConnect
source venv/bin/activate

# Sync everything — last 30 days (default)
python kite_sync.py

# Custom date range
python kite_sync.py --from 2026-03-01 --to 2026-03-22

# Only sync open positions
python kite_sync.py --only positions

# Only sync trades
python kite_sync.py --only trades

# Dry run first — see what would be synced without writing anything
python kite_sync.py --dry-run

# Dry run for a specific range
python kite_sync.py --from 2026-03-01 --dry-run
```

---

## What the script does step by step

**Trades sync:**
```
Kite orders() API
    → filter by date range
    → group COMPLETE orders by symbol + direction + date
    → per group: find existing Trade in DB or create new one
    → upsert OrderEvent rows (skip if exists, update if status changed)
    → auto-detect trade_type (EQUITY / OPTIONS / FUTURES) from exchange
```

**Positions sync:**
```
Kite positions() API  (net + day)
    → for each net position:
        → find or create Instrument
        → find open Trade for that instrument or create placeholder
        → upsert Position row with latest qty, avg price, unrealised P&L
        → if qty = 0 → mark Trade as closed, write P&L