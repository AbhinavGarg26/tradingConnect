#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python_bin="$project_dir/venv/bin/python"
run_dir="$project_dir/tmp/pids"
log_dir="$project_dir/log"

mkdir -p "$run_dir" "$log_dir"

if [[ ! -x "$python_bin" ]]; then
  echo "Python virtual environment not found at $python_bin" >&2
  exit 1
fi

start_service() {
  local name="$1"
  local entrypoint="$2"
  local pid_file="$run_dir/$name.pid"
  local log_file="$log_dir/$name.log"

  if [[ -f "$pid_file" ]]; then
    local existing_pid
    existing_pid="$(<"$pid_file")"
    if kill -0 "$existing_pid" 2>/dev/null; then
      echo "$name already running (PID $existing_pid)"
      return
    fi
    rm -f "$pid_file"
  fi

  local discovered_pid
  discovered_pid="$(pgrep -f "$project_dir/$entrypoint" | head -n 1 || true)"
  if [[ -n "$discovered_pid" ]]; then
    echo "$discovered_pid" > "$pid_file"
    echo "$name already running (PID $discovered_pid); PID file restored"
    return
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

start_service "market_breath" "market_breath.py"
start_service "kite_market_fetcher" "kite_market_fetcher.py"

echo "Both market services are running in the background."
