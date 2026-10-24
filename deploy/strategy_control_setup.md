# Control strategy services from the performance page

The page now provides Schedule future sessions, Start this session, and Pause
future starts, with explicit Paper/Live selection. It supports the two EMA
strategies, PE1-V1, Fixed Profit · Filtered and Nifty 15m Breakout v3. Defaults are paper. A live request
requires a risk acknowledgement and a confirmation. Requests are scoped to the
signed-in account; a root-owned server allowlist independently restricts the
account and unit names. No broker orders are submitted by the Rails controller.

## One-time operator setup

Deploy both projects and migrate Rails, including migration 20261024050000.
First install/review the strategy service/timer templates and status observer
from strategy_operations.md and ema_strategy_activation_steps.md. Leave trading
timers disabled during setup. Preserve any manually configured risk protections.
Do not run PE1 alongside the old 30-minute Reference V3 timer/runner. The host must use systemd.

The web server gets NO sudo access. A separate deployer-owned queue worker can
invoke only a root-installed allowlisted helper. The helper never imports app
code or loads credentials; it changes the five reviewed unit commands and timers.
Services themselves must run as deployer, never root. Review this helper before
installing it; keep its source and config root-owned and non-writable by deployer.

Run these installation commands yourself on the trading server:

```sh
sudo install -o root -g root -m 0755 deploy/strategy_unit_control.py /usr/local/sbin/strategy-unit-control
sudo install -o root -g root -m 0644 deploy/systemd/strategy-control-worker.service /etc/systemd/system/strategy-control-worker.service
```

Create /etc/trading-strategy-control.json with sudo, owner root:root, mode 0644:

```json
{
  "user_id": 975447485,
  "project_dir": "/home/deployer/apps/tradingConnect",
  "service_user": "deployer"
}
```

Using `sudo visudo -f /etc/sudoers.d/strategy-control`, add exactly:

```sudoers
Cmnd_Alias STRATEGY_UNIT_CONTROL = /usr/local/sbin/strategy-unit-control
# Arguments are independently validated against the helper's allowlist.
deployer ALL=(root) NOPASSWD: STRATEGY_UNIT_CONTROL
```

Do not grant arbitrary systemctl, shell or Python sudo permissions. The helper
uses only /usr/bin/systemctl and controlled files beneath /etc/systemd/system.
It accepts numeric account ID, one of five keys, one of three actions and
paper/live only. The root-owned configuration must match the expected host.
If your Linux user/path differs, review and adjust the templates/config/sudoers.

In the Rails application's normal production environment set:

```text
STRATEGY_CONTROL_USER_ID=975447485
```

Restart the Rails web process using your normal deployment procedure. The
controls are hidden from other accounts and remain disabled without that flag.
The queue worker also requires the existing Python database environment and
EMA_USER_ID=975447485 in deploy/ema-strategies.env. Then:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now strategy-control-worker.service
```

Starting the worker can execute queued requests. Inspect strategy_control_requests
first and resolve any obsolete requests. Requests older than five minutes fail
validation; never install/start the worker while an unintended request is queued.
No commands in this guide have been executed automatically as part of the change.

## Using the controls

1. Review operation status, positions and pending orders. For PE1 or 15m Breakout v3 live execution,
   save live-approved protected risk settings on the risk page. The independently
   installed guardian timer is enabled when scheduling either protected live strategy. Starting either
   immediately requires an already healthy guardian heartbeat.
2. Select Paper or Live and Schedule future sessions or Start this session.
   For Live, read and check the risk acknowledgement. Confirm the submission.
3. The page first says Queued, then Processing, then Succeeded/Failed/Uncertain.
   Succeeded means the server control command was accepted, not that a strategy
   has authenticated with the broker or filled an order. Verify the next status
   observation and actual run heartbeat. The page refreshes every 30 seconds.
4. Schedule configures mode and enables the strategy timer. Start configures mode
   and dispatches this session only, leaving the recurring timer paused. Immediate
   starts are rejected outside the runner's startup window; schedule instead.
5. Pause future starts disables the timer only; it never stops the current service
   or sells positions. No stop/liquidate control is provided. An active or changing
   runner blocks mode changes. Open/unresolved trades, EMA intents/checkpoints and
   other existing recovery checks can also block activation.

Mode is written into 90-dashboard.conf under the unit's systemd drop-in folder.
Inspect the effective ExecStart with `systemctl cat NAME.service`. If an older
manual override.conf sets ExecStart, consolidate/remove that command override
during setup so 90-dashboard.conf controls execution; preserve unrelated risk
protections. The helper verifies the effective command after daemon-reload and
refuses to start/enable a timer if another override changes it.
The helper refuses active legacy 30-minute Reference V3 units when configuring PE1. Existing broker
login, calendar checks, budgets and runner-specific entry checks remain enforced.

A helper failure may occur after a partial change (for example mode changed but
timer not enabled). It becomes Uncertain and is NOT automatically retried. A
worker interrupted after claiming a request also becomes Uncertain after five
minutes. Inspect effective unit mode, timer/service state, broker account and
logs; only then mark the request resolved via the normal operator DB procedure.
The partial unique index blocks more requests for that strategy while a request
is queued, processing or uncertain. Do not delete audit records to force a retry.

```sh
sudo journalctl -u strategy-control-worker.service -u strategy-status-observer.service -f
```

The page cannot install Linux units, grant sudo rights, obtain broker login or
validate strategy profitability. Those remain deployment/account prerequisites.
EMA strategies have no fixed protective stop; Fixed Profit index exits require
its live process and fresh quotes. No real activation or trading was performed
while implementing these controls.

## Existing Nifty 15m Breakout v3

This fifth entry uses the existing nifty-15m-v3.service/timer and strategy key
nifty_15m_breakout, version v3. Its existing live ExecStart and timer templates
are left unchanged. The dashboard helper preserves token-attempts=0 and
token-retry-seconds=300 and supplies an explicit --user-id when controlling it.
The runner now accepts that optional account flag; older commands still work.
Live scheduling retains protected risk approval and guardian requirements.
Immediate starts are allowed only 09:15–15:00 IST, with existing runner restart
checks still enforced. No new strategy rules or default risk cap were changed.

If the existing unit omits --user-id, the status observer cannot verify which
account its default token selection will use. Last-run mode/heartbeat remain
visible, but configured mode is Unknown with an account-binding notice. At a
safe inactive time, update the effective command to include --user-id 975447485,
or use a reviewed page request after setup. Do not restart a live runner just
to refresh its label. Reinstall the updated root-owned helper and deploy/restart
the control worker/observer when adding this fifth allowlisted strategy.
