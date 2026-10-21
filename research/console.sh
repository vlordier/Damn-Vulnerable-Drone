#!/usr/bin/env bash
# Manage the checkout-owned, loopback-only Flask research console.
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$PROJECT_ROOT/research/owned_process.sh"
DVD_PROCESS_SUPERVISOR="$(dvd_process_supervisor "$PROJECT_ROOT")"
export DVD_PROCESS_SUPERVISOR
ACTION="${1:-status}"
PORT="${DVD_RESEARCH_UI_PORT:-8001}"

case "$ACTION" in
  start)
    DVD_RESEARCH_UI_PORT="$PORT" uv run --project "$PROJECT_ROOT/research" \
      python "$PROJECT_ROOT/research/manage_console_process.py" start
    for _ in {1..40}; do
      if curl --silent --fail --output /dev/null "http://127.0.0.1:$PORT/attacks/research"; then
        DVD_RESEARCH_UI_PORT="$PORT" uv run --project "$PROJECT_ROOT/research" \
          python "$PROJECT_ROOT/research/manage_console_process.py" status
        exit 0
      fi
      sleep 0.25
    done
    if [[ -f "$PROJECT_ROOT/.research-ui-runtime/console.log" && ! -L "$PROJECT_ROOT/.research-ui-runtime/console.log" ]]; then
      cat "$PROJECT_ROOT/.research-ui-runtime/console.log" >&2
    fi
    echo "Research console did not become ready at http://127.0.0.1:$PORT." >&2
    exit 1
    ;;
  status|stop)
    DVD_RESEARCH_UI_PORT="$PORT" uv run --project "$PROJECT_ROOT/research" \
      python "$PROJECT_ROOT/research/manage_console_process.py" "$ACTION"
    ;;
  *)
    echo "Usage: $0 {start|status|stop}" >&2
    exit 2
    ;;
esac
