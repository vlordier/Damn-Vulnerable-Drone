#!/usr/bin/env bash
# Start research subprocesses under a native parent that owns their process
# group. A waiter observation and group signal are serialized while the child
# remains unreaped, avoiding the macOS ps/kill PID reuse race.

dvd_process_supervisor() {
  local project_root="$1"
  local source="$project_root/research/dvd_research/process_supervisor.c"
  local binary="$project_root/research/.process-supervisor"
  if [[ -L "$binary" ]]; then
    echo "Refusing a symlinked process supervisor binary: $binary" >&2
    return 1
  fi
  if [[ ! -x "$binary" || "$source" -nt "$binary" ]]; then
    local compiler
    compiler="$(command -v clang || command -v cc || true)"
    [[ -n "$compiler" ]] || { echo "a C compiler is required for process supervision" >&2; return 1; }
    local temporary
    temporary="$(mktemp "$project_root/research/.process-supervisor.XXXXXX")" || return 1
    if ! "$compiler" -std=c11 -Wall -Wextra -Werror -O2 "$source" -o "$temporary"; then
      rm -f "$temporary"
      return 1
    fi
    chmod 700 "$temporary"
    mv -f "$temporary" "$binary"
  fi
  printf '%s\n' "$binary"
}

dvd_run_owned_child() {
  local project_root="$1" identity_path="$2"
  shift 2
  local supervisor
  supervisor="$(dvd_process_supervisor "$project_root")" || return
  exec "$supervisor" run "$identity_path" -- "$@"
}

dvd_signal_owned_child() {
  local project_root="$1" pid="$2" identity_path="$3" signal_name="${4:-TERM}"
  [[ "$pid" =~ ^[0-9]+$ && "$signal_name" == TERM && -f "$identity_path" && ! -L "$identity_path" ]] || return 1
  local supervisor
  supervisor="$(dvd_process_supervisor "$project_root")" || return
  "$supervisor" stop "$identity_path"
}

dvd_wait_run_nats_endpoint() {
  local run_dir="$1" launcher_pid="$2" endpoint_file="$1/research-nats-endpoint.json"
  local deadline=$((SECONDS + ${3:-60})) endpoint
  while ((SECONDS < deadline)); do
    if [[ -s "$endpoint_file" ]]; then
      endpoint="$(uv run --no-project python - "$endpoint_file" <<'PY'
import json
import re
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    value = json.load(source)
if value.get("contract") != "antoine.run-nats-endpoint" or value.get("version") != "1.0.0":
    raise SystemExit("unsupported NATS endpoint receipt")
port = value.get("port")
pid = value.get("pid")
url = value.get("url")
if value.get("host") != "127.0.0.1" or type(port) is not int or not 1 <= port <= 65535:
    raise SystemExit("invalid local NATS endpoint receipt")
if type(pid) is not int or pid < 1 or url != f"nats://127.0.0.1:{port}":
    raise SystemExit("invalid NATS endpoint identity")
print(f"{port} {pid} {url}")
PY
)" || return 1
      local port pid url
      read -r port pid url <<<"$endpoint"
      if [[ -s "$run_dir/logs/nats.log" ]] &&
        rg -Fq "Listening for client connections on 127.0.0.1:$port" "$run_dir/logs/nats.log" &&
        kill -0 "$pid" 2>/dev/null &&
        nats --no-context --server "$url" rtt >/dev/null 2>&1; then
        printf '%s\n' "$url"
        return 0
      fi
      echo "NATS endpoint receipt exists but its run-owned process is not ready." >&2
      return 1
    fi
    if ! kill -0 "$launcher_pid" 2>/dev/null; then
      echo "Flight launcher exited before publishing its NATS endpoint." >&2
      return 1
    fi
    sleep 0.2
  done
  echo "Timed out waiting for the flight run's NATS endpoint receipt." >&2
  return 1
}

dvd_install_exit_cleanup_traps() {
  local cleanup_function="${1:-}"
  [[ "$cleanup_function" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]] || {
    echo "cleanup handler must be a shell function name" >&2
    return 2
  }
  declare -F "$cleanup_function" >/dev/null || {
    echo "cleanup handler is not defined: $cleanup_function" >&2
    return 2
  }
  trap "$cleanup_function" EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
}
