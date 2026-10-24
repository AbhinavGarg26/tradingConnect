# Resolving the 24 October production findings

## Tested code repairs

- Breakout historical reads are scheduled once per new completed 15m candle,
  instead of every five seconds. HTTP 429 on historical reads backs off 10, 20,
  then up to 40 seconds; a bounded outage halts and preserves existing stale-bar
  protections. No order call is retried by this mechanism.
- A delayed/unpublished latest bar is retried instead of being marked consumed.
- A missing local user_id in the live risk-gate path now uses recorder.user_id.
- Idle polls keep the DB heartbeat fresh.
- EMA warmup loads at most 30 calendar days of raw minutes at a time and preserves
  the original 2020 seed, exact aggregated bars and full historical EMA order.
- EMA snapshot generation retains only pending checkpoint catch-up rows plus the
  latest state, and the order executor retains only NIFTY instrument metadata.

## Operator actions that change trading configuration

These actions are not run by the code repair/audit. Review actual broker state
before applying changes. The four new strategy timers remain paper/disabled
until you explicitly choose a mode and schedule them.

1. Retire only the old 30m Reference V3 schedule before using PE1:

```sh
sudo systemctl disable --now nifty-30m-reference-v3.timer
```

This timer is different from nifty-15m-v3.timer. Disabling the old timer does not
stop its already-running service or close any position. Confirm that its runner
is inactive and positions/orders are reconciled first.

2. Bind the existing 15m Breakout unit explicitly to the account. The updated
source template adds EnvironmentFile and --user-id while retaining --live and
the existing token retry flags. At a safe inactive time, review/install it:

```sh
sudo install -m 0644 /home/deployer/apps/tradingConnect/deploy/systemd/nifty-15m-v3.service /etc/systemd/system/nifty-15m-v3.service
sudo systemctl daemon-reload
```

Review `systemctl cat nifty-15m-v3.service` for any overriding ExecStart. Do not
restart the live runner merely to update its status label. The existing timer
will use the effective command at its next start; this is a live configuration
change for the operator to apply explicitly.

3. For each new strategy, use Strategies → Manage strategy. Review risk and
readiness, choose Paper or Live, then Schedule future sessions and confirm.
A live request requires the risk acknowledgement. PE1/Breakout require saved
protected risk approval and the guardian. No row deletion or checkpoint reset
is part of this repair.

## Verification

Read-only audits and --check commands do not establish order fills. Run the EMA
checks sequentially with a safe memory limit; do not run the original full-minute
warmup implementation on the small production host. Verify current broker
session, calendar, source candles and guardian at the intended session start.
