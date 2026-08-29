# ADX Trading System

Modular ADX-based strategy system for NSE/Kite Connect.

## Architecture

```
adx_system/
├── indicators/
│   ├── adx.py              ← ADX, +DI, -DI calculation (Wilder smoothing)
│   └── ema.py              ← Multi-period EMA
├── strategies/
│   ├── adx_crossover.py    ← PRIMARY: ADX rising from ≤29 + DI gap widening
│   └── adx_trend_strength.py  ← SECONDARY: ADX >25 + EMA pullback re-entry
├── scanner/
│   └── adx_scanner.py      ← Scans instrument list, prints candidates
└── backtest/
    └── adx_backtest.py     ← Full backtest with P&L, win rate, trade log
```

---

## Strategies

### 1. ADX Crossover (Primary — Your Setup)

**Logic:**
- ADX was ≤ 29 on the prior bar → market was flat/weak
- ADX is now rising (crossing up)
- +DI is above -DI AND the gap is **widening** → **BUY**
- -DI is above +DI AND the gap is **widening** → **SELL**
- Optional EMA50 filter: only BUY above EMA50, SELL below

**Why it works:** Markets spend most time in ranges (ADX < 25–30). When ADX
starts climbing from a low base while one DI clearly dominates and the gap
expands, it signals a new trend is **just starting** — the best time to enter.

**SL:** 1.5x ATR from entry  
**Target:** 2:1 R:R minimum

---

### 2. ADX Trend Strength (Secondary — Re-entry)

**Logic:**
- ADX > 25 AND still rising over the last 2 bars
- DI spread ≥ 8 (strong directional commitment)
- Price pulls back to EMA21 zone (within 1.5x ATR)

**Why it works:** After the crossover signal fires, a trend often runs,
then consolidates briefly at the EMA. This catches the second leg without
chasing.

**SL:** Below EMA21 − 0.5x ATR  
**Target:** 2.5:1 R:R

---

## CLI Usage

### Run Scanner (find today's candidates)
```bash
# Find ADX crossover candidates across all your instruments
python scanner/adx_scanner.py --strategy crossover --interval day --lookback 120

# Find trend-continuation pullback setups
python scanner/adx_scanner.py --strategy trend --interval day

# 15-min intraday scan (run during market hours)
python scanner/adx_scanner.py --strategy crossover --interval 15minute --lookback 50
```

### Run Backtest (validate on history)
```bash
# Backtest RELIANCE on daily charts, 1 year
python backtest/adx_backtest.py --symbol RELIANCE --strategy crossover --lookback 365

# Backtest BankNifty on 60-min, save trade log
python backtest/adx_backtest.py --symbol BANKNIFTY --exchange NFO \
    --strategy crossover --interval 60minute --lookback 180 --save-csv

# Test trend-strength pullback strategy
python backtest/adx_backtest.py --symbol HDFCBANK --strategy trend --lookback 365
```

---

## Programmatic Usage

```python
from dotenv import load_dotenv
load_dotenv()

from trading.user_token import fetch_user_token
kite, user_id = fetch_user_token(log)

# ── Quick scan ────────────────────────────────────────────────
from scanner.adx_scanner import ADXScanner

instruments = [
    {"instrument_token": 738561, "tradingsymbol": "RELIANCE"},
    {"instrument_token": 341249, "tradingsymbol": "HDFCBANK"},
]
scanner    = ADXScanner(kite, strategy="crossover", interval="day")
candidates = scanner.scan(instruments)
scanner.print_candidates(candidates)

# ── Backtest a specific stock ──────────────────────────────────
import pandas as pd
from datetime import datetime, timedelta
from backtest.adx_backtest import ADXBacktest

raw = kite.historical_data(738561, "2024-01-01", "2025-01-01", "day")
df  = pd.DataFrame(raw).rename(columns={"date": "datetime"}).set_index("datetime")

bt     = ADXBacktest(strategy="crossover", adx_floor=29)
result = bt.run(df, symbol="RELIANCE")
bt.print_report(result)

# ── Use indicators standalone ─────────────────────────────────
from indicators.adx import compute_adx
df = compute_adx(df, period=14)
print(df[["close", "adx", "plus_di", "minus_di"]].tail(5))
```

---

## Configuration

| Parameter     | Default | Description |
|---------------|---------|-------------|
| `adx_floor`   | 29      | Max ADX on prior bar for crossover |
| `adx_period`  | 14      | ADX lookback (standard Wilder) |
| `atr_sl_mult` | 1.5     | ATR multiplier for stop-loss |
| `rr_target`   | 2.0     | Reward:Risk for target |
| `ema_filter`  | True    | Filter signals by EMA50 trend |
| `min_di_gap`  | 3.0     | Min DI spread to accept signal |

---

## Drop-in Integration with Your Existing System

The scanner uses the same `resources/instrument_list.csv` path your
`instrument_sync.py` already maintains. No schema changes needed.

Add a cron job or rake task to run the scanner before market open:
```bash
# In your Rails app or crontab:
# 9:00 AM every weekday
0 9 * * 1-5 cd /path/to/project && python scanner/adx_scanner.py \
    --strategy crossover --interval day >> logs/adx_scanner.log 2>&1
```