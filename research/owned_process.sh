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
