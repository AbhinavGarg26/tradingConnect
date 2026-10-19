#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Production uses this one checkout for both managed services. Do not restart
# the live strategy while it may hold an option position; its in-memory state
# cannot be reconstructed safely by a fresh process.
if [[ "$project_dir" == "/home/deployer/apps/tradingConnect" ]] \
    && systemctl cat nifty-15m-v3.timer >/dev/null 2>&1; then
  if [[ $# -ne 0 ]]; then
    echo "Usage: $0" >&2
    exit 2
  fi
  if sudo -n systemctl is-active --quiet nifty-15m-v3.service; then
    echo "NIFTY live strategy is active; refusing to deploy or restart it mid-session." >&2
    exit 1
  fi
  python_bin="$project_dir/.venv/bin/python"
  if [[ ! -x "$python_bin" ]]; then
    echo "Python virtual environment not found at $python_bin" >&2
    exit 1
  fi
  "$python_bin" -m compileall -q "$project_dir/nifty_15m_live.py" \
    "$project_dir/trading/strategy_records.py" "$project_dir/kite_market_fetcher.py" \
    "$project_dir/market_breath.py"
  if ! systemctl cat market-breath.timer >/dev/null 2>&1; then
    echo "market-breath.timer is not installed; ask an administrator to install deploy/systemd units." >&2
    exit 1
  fi
  # The timer, rather than boot, owns Market Breath startup. Do not stop an
  # already-running session during a deployment; it will be timer-started on
  # the next weekday at 09:15 IST.
  sudo -n systemctl disable market-breath.service
  sudo -n systemctl enable --now market-breath.timer
  sudo -n systemctl restart kite-market-fetcher.service
  sudo -n systemctl is-active --quiet kite-market-fetcher.service
  sudo -n systemctl enable --now nifty-15m-v3.timer
  echo "Kite fetcher restarted from $project_dir ($(git -C "$project_dir" rev-parse --short HEAD))."
  echo "Market Breath and NIFTY v3 are scheduled for the next 09:15 IST weekday start."
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
