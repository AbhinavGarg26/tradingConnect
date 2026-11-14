#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Production uses one checkout for the managed services. Active strategies are
# never restarted: their in-memory position state cannot be reconstructed
# safely. A deploy shortly after the opening bell may, however, recover a timer
# that was missed while the host was down.
if [[ "$project_dir" == "/home/deployer/apps/tradingConnect" \
      || "$project_dir" == "/home/ubuntu/tradingConnect" ]] \
    && systemctl cat nifty-15m-v3.timer >/dev/null 2>&1; then
  if [[ $# -ne 0 ]]; then
    echo "Usage: $0" >&2
    exit 2
  fi
  python_bin="$project_dir/.venv/bin/python"
  if [[ ! -x "$python_bin" ]]; then
    echo "Python virtual environment not found at $python_bin" >&2
    exit 1
  fi
  "$python_bin" -m compileall -q "$project_dir/nifty_15m_live.py" \
    "$project_dir/nifty_30m_reference_live.py" \
    "$project_dir/fixed_profit_filtered_live.py" \
    "$project_dir/ema_strategy_runner.py" \
    "$project_dir/strategy_guardian.py" "$project_dir/trading/strategy_risk.py" \
    "$project_dir/trading/strategy_records.py" "$project_dir/kite_market_fetcher.py" \
    "$project_dir/market_breath.py" "$project_dir/strategy_day_end.py" \
    "$project_dir/one_direction_profit_lock_service.py" \
    "$project_dir/one_direction_profit_lock_readiness.py" \
    "$project_dir/trading/one_direction_profit_lock_journal.py" \
    "$project_dir/trading/one_direction_profit_lock_execution.py"

  strategy_units=(
    nifty-15m-v3
    nifty-30m-pe1
    fixed-profit-filtered
    nifty-bearish-15m-ema
    nifty-bullish-15m-ema
    nifty-daily-ema-calls
    one-direction-profit-lock
  )
  required_timers=(market-breath strategy-guardian strategy-status-observer strategy-day-end "${strategy_units[@]}")
  for unit in "${required_timers[@]}"; do
    if ! systemctl cat "$unit.timer" >/dev/null 2>&1; then
      echo "$unit.timer is not installed; ask an administrator to install deploy/systemd units." >&2
      exit 1
    fi
  done
  ist_weekday="$(TZ=Asia/Kolkata date +%u)"
  ist_clock="$(TZ=Asia/Kolkata date +%H%M)"
  ist_day="$(TZ=Asia/Kolkata date +%Y-%m-%d)"

  # Keep boot disabled, but retain the 09:15 timer for normal daily starts.
  # A deployment is also the manual recovery path if that timer was missed.
  sudo -n systemctl disable market-breath.service
  sudo -n systemctl enable --now market-breath.timer
  # Market Breath exits normally after its session window. Restart it only
  # while it can remain active; its timer owns the next scheduled start.
  if (( ist_weekday <= 5 )) && [[ "$ist_clock" > "0859" && "$ist_clock" < "1541" ]]; then
    # Use the service alias permitted to the deployment account by sudoers.
    sudo -n systemctl restart market-breath
    sudo -n systemctl is-active --quiet market-breath.service
  fi
  sudo -n systemctl restart kite-market-fetcher.service
  sudo -n systemctl is-active --quiet kite-market-fetcher.service
  sudo -n systemctl enable --now strategy-guardian.timer strategy-status-observer.timer \
    strategy-day-end.timer "${strategy_units[@]/%/.timer}"

  # Recover only a genuinely missed opening timer. Never restart a service that
  # is active or rerun one whose timer already fired today. The 09:45 cutoff is
  # shared with the strictest runner (Fixed Profit), preventing stale catch-up
  # entries later in the session.
  recovered=()
  if (( ist_weekday <= 5 )) && [[ "$ist_clock" > "0913" && "$ist_clock" < "0946" ]]; then
    for unit in "${strategy_units[@]}"; do
      if systemctl is-active --quiet "$unit.service"; then
        continue
      fi
      last_trigger="$(systemctl show "$unit.timer" --property=LastTriggerUSec --value)"
      if [[ "$last_trigger" == *"$ist_day"* ]]; then
        continue
      fi
      sudo -n systemctl start "$unit.service"
      recovered+=("$unit")
    done
  fi

  echo "Market Breath and Kite fetcher restarted from $project_dir ($(git -C "$project_dir" rev-parse --short HEAD))."
  echo "All strategy, guardian, observer, and day-end timers are enabled."
  if ((${#recovered[@]})); then
    echo "Recovered missed opening services: ${recovered[*]}."
  else
    echo "No missed opening strategy services required recovery."
  fi
  exit 0
fi

if [[ -x "$project_dir/.venv/bin/python" ]]; then
  python_bin="$project_dir/.venv/bin/python"
else
  python_bin="$project_dir/venv/bin/python"
fi
run_dir="$project_dir/tmp/pids"
log_dir="$project_dir/log"

mkdir -p "$run_dir" "$log_dir"

if [[ ! -x "$python_bin" ]]; then
  echo "Python virtual environment not found at $python_bin" >&2
  exit 1
fi

restart_service() {
  local name="$1"
  local entrypoint="$2"
  local pid_file="$run_dir/$name.pid"
  local log_file="$log_dir/$name.log"

  if [[ -f "$pid_file" ]]; then
    local existing_pid
    existing_pid="$(<"$pid_file")"
    if kill -0 "$existing_pid" 2>/dev/null; then
      echo "Stopping $name (PID $existing_pid)..."
      kill "$existing_pid"
      for _ in {1..20}; do
        kill -0 "$existing_pid" 2>/dev/null || break
        sleep 0.25
      done
      if kill -0 "$existing_pid" 2>/dev/null; then
        echo "$name did not stop cleanly; refusing to start a duplicate" >&2
        exit 1
      fi
    fi
    rm -f "$pid_file"
  fi

  local discovered_pid
  discovered_pid="$(pgrep -f "$project_dir/$entrypoint" | head -n 1 || true)"
  if [[ -n "$discovered_pid" ]]; then
    echo "Stopping discovered $name process (PID $discovered_pid)..."
    kill "$discovered_pid"
    for _ in {1..20}; do
      kill -0 "$discovered_pid" 2>/dev/null || break
      sleep 0.25
    done
    if kill -0 "$discovered_pid" 2>/dev/null; then
      echo "$name did not stop cleanly; refusing to start a duplicate" >&2
      exit 1
    fi
  fi

  (
    cd "$project_dir"
    nohup "$python_bin" "$entrypoint" >> "$log_file" 2>&1 &
    echo "$!" > "$pid_file"
  )

  local started_pid
  started_pid="$(<"$pid_file")"
  sleep 1
  if kill -0 "$started_pid" 2>/dev/null; then
    echo "$name started (PID $started_pid, log $log_file)"
  else
    echo "$name failed to start; inspect $log_file" >&2
    exit 1
  fi
}

restart_service "market_breath" "market_breath.py"
restart_service "kite_market_fetcher" "kite_market_fetcher.py"

echo "Both market services are running in the background."
