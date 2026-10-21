#!/usr/bin/env bash
# Replay one captured current setpoint on a single isolated local flight.
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$PROJECT_ROOT/research/owned_process.sh"
REPORT_ROOT="${ANTOINE_REPORT_DIR:-$PROJECT_ROOT/build_logs/research/antoine-scenarios}"
mkdir -p "$REPORT_ROOT"
RUN_ID="antoine-waypoint-replay-$(date -u +%Y%m%dT%H%M%SZ)-$$"
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
ATTACK_PID=""
FLIGHT_IDENTITY="$REPORT_ROOT/.$RUN_ID-flight.identity"
ATTACK_IDENTITY="$REPORT_ROOT/.$RUN_ID-attack.identity"
cleanup() {
  local status=$?
  [[ -z "$ATTACK_PID" ]] || dvd_signal_owned_child "$PROJECT_ROOT" "$ATTACK_PID" "$ATTACK_IDENTITY" TERM || true
  [[ -z "$FLIGHT_PID" ]] || dvd_signal_owned_child "$PROJECT_ROOT" "$FLIGHT_PID" "$FLIGHT_IDENTITY" TERM || true
  [[ -z "$ATTACK_PID" ]] || wait "$ATTACK_PID" 2>/dev/null || true
  [[ -z "$FLIGHT_PID" ]] || wait "$FLIGHT_PID" 2>/dev/null || true
  rm -f "$ATTACK_IDENTITY" "$FLIGHT_IDENTITY"
  return "$status"
}
trap cleanup EXIT INT TERM

dvd_run_owned_child "$PROJECT_ROOT" "$FLIGHT_IDENTITY" env DVD_LAUNCHER_MANAGED=1 DVD_LAUNCHER_IDENTITY="$FLIGHT_IDENTITY" "$PROJECT_ROOT/scripts/run-antoine.sh" \
  --run-dir "$RUN_DIR" --flight-seconds 45 --settle-seconds 10 --keep \
  >"$REPORT_ROOT/$RUN_ID-launcher.log" 2>&1 &
FLIGHT_PID=$!
dvd_run_owned_child "$PROJECT_ROOT" "$ATTACK_IDENTITY" uv run --no-project python "$PROJECT_ROOT/research/antoine_setpoint_replay.py" \
  --server "$SERVER" --run-dir "$RUN_DIR" \
  >"$REPORT_ROOT/$RUN_ID-replay.log" 2>&1 &
ATTACK_PID=$!

set +e
wait "$FLIGHT_PID"
FLIGHT_STATUS=$?
FLIGHT_PID=""
rm -f "$FLIGHT_IDENTITY"
wait "$ATTACK_PID"
REPLAY_STATUS=$?
ATTACK_PID=""
rm -f "$ATTACK_IDENTITY"
set -e

if [[ ! -f "$RUN_DIR/research-provenance-comparison.json" ]]; then
  echo "Postflight provenance comparison is missing; replay qualification will fail closed." >&2
fi
set +e
uv run --no-project python "$PROJECT_ROOT/research/summarize_antoine_replay.py" "$RUN_DIR"
SUMMARY_STATUS=$?
set -e
printf 'replay run: %s\n' "$RUN_DIR"
printf 'flight proof exit: %s\n' "$FLIGHT_STATUS"
printf 'replay observation exit: %s\n' "$REPLAY_STATUS"
cat "$REPORT_ROOT/$RUN_ID-launcher.log"
cat "$REPORT_ROOT/$RUN_ID-replay.log"
[[ "$REPLAY_STATUS" -eq 0 && "$FLIGHT_STATUS" -eq 0 && "$SUMMARY_STATUS" -eq 0 ]]
