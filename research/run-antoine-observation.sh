#!/usr/bin/env bash
# Measure read access to selected subjects on one isolated local Antoine run.
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$PROJECT_ROOT/research/owned_process.sh"
REPORT_ROOT="${ANTOINE_REPORT_DIR:-$PROJECT_ROOT/build_logs/research/antoine-scenarios}"
mkdir -p "$REPORT_ROOT"
RUN_ID="antoine-nats-observation-$(date -u +%Y%m%dT%H%M%SZ)-$$"
RUN_DIR="$REPORT_ROOT/$RUN_ID"
[[ ! -e "$RUN_DIR" ]] || { echo "Refusing to overwrite evidence: $RUN_DIR" >&2; exit 1; }

for tool in nats uv; do
  command -v "$tool" >/dev/null 2>&1 || { echo "Required command not found: $tool" >&2; exit 1; }
done
UV_CACHE_DIR="${UV_CACHE_DIR:-/private/tmp/dvd-antoine-uv-cache}" UV_OFFLINE=1
export UV_CACHE_DIR UV_OFFLINE
if [[ -z "${ANTOINE_GZ_FLIGHT_NATS_PORT:-}" ]]; then
  ANTOINE_GZ_FLIGHT_NATS_PORT="$(uv run --no-project python -c \
    'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
fi
export ANTOINE_GZ_FLIGHT_NATS_PORT
SERVER="nats://127.0.0.1:$ANTOINE_GZ_FLIGHT_NATS_PORT"

FLIGHT_PID=""
OBSERVER_PID=""
FLIGHT_IDENTITY="$REPORT_ROOT/.$RUN_ID-flight.identity"
OBSERVER_IDENTITY="$REPORT_ROOT/.$RUN_ID-observer.identity"
cleanup() {
  local status=$?
  [[ -z "$OBSERVER_PID" ]] || dvd_signal_owned_child "$PROJECT_ROOT" "$OBSERVER_PID" "$OBSERVER_IDENTITY" TERM || true
  [[ -z "$FLIGHT_PID" ]] || dvd_signal_owned_child "$PROJECT_ROOT" "$FLIGHT_PID" "$FLIGHT_IDENTITY" TERM || true
  [[ -z "$OBSERVER_PID" ]] || wait "$OBSERVER_PID" 2>/dev/null || true
  [[ -z "$FLIGHT_PID" ]] || wait "$FLIGHT_PID" 2>/dev/null || true
  rm -f "$OBSERVER_IDENTITY" "$FLIGHT_IDENTITY"
  return "$status"
}
dvd_install_exit_cleanup_traps cleanup

dvd_run_owned_child "$PROJECT_ROOT" "$FLIGHT_IDENTITY" env DVD_LAUNCHER_MANAGED=1 DVD_LAUNCHER_IDENTITY="$FLIGHT_IDENTITY" "$PROJECT_ROOT/scripts/run-antoine.sh" \
  --run-dir "$RUN_DIR" --flight-seconds 35 --settle-seconds 8 --keep \
  >"$REPORT_ROOT/$RUN_ID-launcher.log" 2>&1 &
FLIGHT_PID=$!

dvd_run_owned_child "$PROJECT_ROOT" "$OBSERVER_IDENTITY" uv run --no-project python "$PROJECT_ROOT/research/antoine_nats_observation.py" \
  --server "$SERVER" --run-dir "$RUN_DIR" \
  >"$REPORT_ROOT/$RUN_ID-observation.log" 2>&1 &
OBSERVER_PID=$!

set +e
wait "$OBSERVER_PID"
OBSERVER_STATUS=$?
OBSERVER_PID=""
rm -f "$OBSERVER_IDENTITY"
wait "$FLIGHT_PID"
FLIGHT_STATUS=$?
FLIGHT_PID=""
rm -f "$FLIGHT_IDENTITY"
set -e

if [[ ! -f "$RUN_DIR/research-provenance-comparison.json" ]]; then
  echo "Postflight provenance comparison is missing; observation classification will fail closed." >&2
fi
uv run --no-project python "$PROJECT_ROOT/research/finalize_antoine_observation.py" \
  "$RUN_DIR" --flight-exit "$FLIGHT_STATUS"

printf 'observation run: %s\n' "$RUN_DIR"
printf 'observer exit: %s\n' "$OBSERVER_STATUS"
printf 'flight proof exit: %s\n' "$FLIGHT_STATUS"
cat "$REPORT_ROOT/$RUN_ID-observation.log"
cat "$REPORT_ROOT/$RUN_ID-launcher.log"
[[ "$OBSERVER_STATUS" -eq 0 ]]
