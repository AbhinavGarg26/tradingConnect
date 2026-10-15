# NIFTY 15-minute breakout

Current live strategy identity: `nifty_15m_breakout`, version `v3`.

`Nifty15mBreakout.on_bar()` is the sole decision function used by the CSV
backtest and the live candle adapter. The immediately previous completed
15-minute NIFTY index candle supplies resistance (high) and support (low).
A later candle close strictly above resistance signals BUY_CE; a close strictly
below support signals BUY_PE. Only one position is tracked at a time.
The first qualifying breakout can enter normally. After any exit, a new CE
must close above both the immediately previous candle's high and the previous
entry candle's high; a new PE must close below both corresponding lows. The
prior entry candle is the candle that generated the last BUY signal. This
fresh-break condition resets at the start of each trading day.
The engine accepts at most **four entries per trading day**. Further breakout
signals are logged as `trade_limit` and cannot place an option order.

For CE, the index stop is previous low minus 5 points and the index target is
entry signal close plus 30 points. For PE, the index stop is previous high plus
5 points and the target is entry signal close minus 30 points. Exits are
evaluated at subsequent 15-minute closes. Open positions are also closed at
the 15:15 close of the 15:00 candle; no new entry is made then. The reference
range resets each trading day. The exit fill is **not** assumed to equal the
stop or target level.

An entry is skipped by default when the distance from the signal close to its
initial NIFTY stop exceeds 30 points. This keeps the planned index-point risk
no larger than the 30-point target at the signal price. The actual next-open
fill and 15-minute close exit can still produce a larger loss.
Starting with the second entry of each day (and for later entries), the stop
trails in 10-point steps based on **completed candle closes**: when NIFTY has
moved 10 favorable points from the entry signal close, the index stop moves
to +10 points of locked profit; at +20 it moves to +20, and so on. The stop
never moves backward. CE and PE are symmetric. This does not guarantee a
+10-point realized gain: an exit is checked on a later 15-minute close and
filled at the following open in the backtest, so price can gap past the stop.
The first trade of each day keeps its original fixed stop.

The standalone runner buys **one NIFTY option exchange lot** per entry, not
one individual option unit. It reads the current `lot_size` from Kite's
instrument list. Live mode remains opt-in via `--live`; the default is paper.

Run a signal backtest with a genuine 15-minute index CSV:

```sh
venv/bin/python -m backtest.nifty_15m_breakout path/to/nifty_15m.csv
```

Or fetch and backtest directly from Kite using the active user's session in
the project's database:

```sh
venv/bin/python -m backtest.nifty_15m_breakout --fetch --from-date 2026-09-01 --to-date 2026-09-30
```

Add `--summary-only` to either command to print only the performance summary.
The summary reports completed trades, wins/losses, gross and net index points,
expectancy per trade, profit factor, payoff ratio, average initial risk,
average R, closed-trade drawdown, CE/PE results and exit-reason results.
The last line highlights `FINAL P&L` in NIFTY index points. It is not a
rupee or option-premium P&L.
By default, the backtest estimates fills at the next 15-minute candle's open,
after the signal candle closes. Use `--fill-model signal-close` to reproduce
the earlier optimistic report. `--slippage-points 1` applies one adverse index
point to each entry and exit fill as a stress test, not an option-price model.
The 30-point cap is the default in both backtest and live runner. Use
`--no-risk-cap` for the original uncapped rule, or `--max-risk-points N` to
try another cap. These switches do not change any other strategy rule.
For example:

```sh
venv/bin/python -m backtest.nifty_15m_breakout --fetch --from-date 2026-09-01 --to-date 2026-09-30 --summary-only --max-risk-points 30 --slippage-points 1
```

When a risk cap is active, the report also prints the uncapped result for
the same dates and fill assumptions. A better result on those same dates may
reflect sample fitting; test later dates separately before using the cap live.

Required columns: `date,open,high,low,close`, ordered oldest to newest.
The repository's `resources/historical_data/NIFTY 50.csv` has daily bars,
so it must not be used for this strategy. This backtest reports index signal
levels only. Actual option return requires historical contract quotes/fills,
expiry and strike selection, fees, and slippage.

For a live candle stream, instantiate `Nifty15mLiveAdapter(nifty_token,
execute_callback)` and register `adapter.on_candle_close` with the existing
WebSocket engine. The engine must build 15-minute candles and subscribe to the
NIFTY index token. The callback must map BUY_CE/BUY_PE to a selected NFO
contract, confirm fills and exits, and recover any position after a restart.
This adapter deliberately does not place orders by itself: those contract and
execution choices are not specified by the strategy rule.

For the standalone Kite runner, keep the project's `.env` and a valid active
user Kite session in the database, then run from the repository root. On a
local checkout, the NIFTY commands use the sibling Rails project's
`config/.env.local` database username/password when connecting to the
matching database through the SSH tunnel on `127.0.0.1:5433`. If that tunnel
is not running, start it from the repository root with:

```sh
POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=5433 ../portfolio-tracker/bin/postgres-tunnel
```

Then run the paper runner:

```sh
venv/bin/python nifty_15m_live.py
```

Before either runner mode, apply the Rails migration in `portfolio-tracker`
that creates `strategy_runs`, `strategy_trades` and `strategy_events`:

```sh
cd ../portfolio-tracker && bin/rails db:migrate
```

The run table records mode, strategy/version, start, heartbeat and failure.
The trade table records the selected option, one-lot quantity, entry and exit
signal times, reasons, NIFTY stops/target, Kite order IDs and confirmed option
premium fills in live mode. The event table records entries, exits, stop
trails, risk-cap skips and trade-limit skips. A pending or uncertain order is
marked for manual reconciliation. Paper trades have no option-premium or ₹
P&L; their index points use signal closes, not next-open fills. Live option
P&L is gross premium difference times contract quantity, before charges.
The Rails page at `/strategy_performance` shows the journal and per-version
closed-trade summaries for the signed-in user.

This is paper mode: it logs the actual NIFTY signals and selected option.
It selects the NIFTY expiry in the *next calendar week*, chooses the strike
nearest the NIFTY index at entry, and uses the option contract's `lot_size`
for one lot. Once paper mode is checked, real orders require the explicit flag:

```sh
venv/bin/python nifty_15m_live.py --live
```

The live runner polls Kite's 15-minute historical candles, waits five seconds
past the bar boundary, and does not replay earlier signals when it starts.
Start either runner mode before the 09:30 candle closes; a later startup is
rejected because the earlier entry/re-entry state cannot be safely recovered.
It confirms order status before continuing. It refuses to start when an open
NIFTY option position or unresolved tagged order exists. If it stops while a
position is open, reconcile and close that position manually before starting
again. Keep it running and supervised during market hours: the NIFTY-index
stop and target are checked only at 15-minute closes, so there is no broker
native option-premium stop between closes.
