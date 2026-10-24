# Persistent EMA calculations

Daily 20/50 calls and 15-minute 9/21 puts share deterministic indicator caches in the application database. The calculation history is separate from positions, orders and execution checkpoints.

- `strategy_indicator_caches`: configuration fingerprint, latest EMA values, sample count, historical-source watermark and last processed candle.
- `strategy_indicator_candles`: completed candle OHLC, calculated EMAs and crossover flags, unique by configuration and candle time.

The first build calculates the existing frozen history from 2020-01-01 in bounded chunks. Later starts load the saved state and only missing sessions. Intraday puts add newly completed 15-minute candles; a restart validates existing candles without counting them twice. The formulas, warmup and existing incomplete-session exclusions are unchanged. Users and paper/live modes share price calculations, while retaining separate execution records.

Daily signals use the previous completed session. The daily runner waits for the opening execution window, evaluates entry/exit/roll and recovery, then exits after confirming no pending signal or unresolved order intent. It does not recalculate throughout the day. Its existing daily timer can remain at 09:14 IST. A failed or interrupted run retains the existing order recovery protections.

## Install and build

Apply the targeted Rails migration using the production application's existing credentials/environment:

```bash
RAILS_ENV=production bundle exec rails db:migrate:up VERSION=20261024060000
```

Then, from the Python project with its existing environment:

```bash
.venv/bin/python ema_indicator_cache.py --strategy all
```

This command uses database candles only and cannot submit broker orders. Repeating it reuses the stored calculations. Readiness checks (`ema_strategy_runner.py --check`) never write cache or execution records; a cold readiness check may still calculate in memory, so build the cache first.

Changes to frozen calculation configuration or the holiday calendar use a separate fingerprint. A detected change in historical source timestamps stops reuse rather than silently changing the frozen history. After reviewing a source correction, explicitly rebuild:

```bash
.venv/bin/python ema_indicator_cache.py --strategy all --rebuild
```

Rebuild only with the affected strategy runners stopped. It changes indicator history, not trades or execution checkpoints. A concurrent historical update aborts the cache transaction; retry once source updates have finished. Trading activation and service scheduling remain separate operator actions.
