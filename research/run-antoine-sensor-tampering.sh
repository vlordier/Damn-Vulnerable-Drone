#!/usr/bin/env bash
# Run one bounded signed-GPS mutation/freshness/replay case in local Antoine.
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$PROJECT_ROOT/research/owned_process.sh"
GZ_BIN="${GZ_BIN:-$HOME/Work/gz-macOS/ws/install/bin/gz}"
REPORT_ROOT="${ANTOINE_REPORT_DIR:-$PROJECT_ROOT/build_logs/research/antoine-scenarios}"
mkdir -p "$REPORT_ROOT"
CASE="${1:-signature-mutation}"
case "$CASE" in
  signature-mutation|duplicate-sequence|out-of-order-sequence|stale-sample|future-sample) ;;
  *) echo "Unsupported GPS experiment case: $CASE" >&2; exit 2 ;;
esac
RUN_ID="antoine-gps-${CASE}-$(date -u +%Y%m%dT%H%M%SZ)-$$"
RUN_DIR="$REPORT_ROOT/$RUN_ID"
[[ ! -e "$RUN_DIR" ]] || { echo "Refusing to overwrite research evidence: $RUN_DIR" >&2; exit 1; }

for tool in nats uv; do
  command -v "$tool" >/dev/null 2>&1 || { echo "Required command not found: $tool" >&2; exit 1; }
done
if [[ -z "${ANTOINE_GZ_FLIGHT_NATS_PORT:-}" ]]; then
  ANTOINE_GZ_FLIGHT_NATS_PORT="$(uv run --no-project python -c \
    'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
fi
export ANTOINE_GZ_FLIGHT_NATS_PORT
export ANTOINE_GZ_SENSOR_FORWARDING=1
export ANTOINE_GZ_CAMERA_RENDERING=0
export FURIA_GZ_NATS_BRIDGE_TARGET_DIR="${FURIA_GZ_NATS_BRIDGE_TARGET_DIR:-${AGR_SIM:-$HOME/Work/durandal-gz-swarm-sim}/target/gz-nats-bridge}"
export FURIA_GZ_NATS_BRIDGE_SOURCE_ROOT="${FURIA_GZ_NATS_BRIDGE_SOURCE_ROOT:-${AGR_SIM:-$HOME/Work/durandal-gz-swarm-sim}}"
export FURIA_GZ_SENSOR_FORWARDER_BIN="${FURIA_GZ_SENSOR_FORWARDER_BIN:-$FURIA_GZ_NATS_BRIDGE_TARGET_DIR/debug/gz-nav-sensor-forwarder}"
if [[ -z "${FURIA_GZ_SENSOR_SIGNING_SEED_HEX:-}" ]]; then
  mapfile -t SENSOR_KEY_MATERIAL < <(uv run --no-project --with pynacl python -c 'import hashlib,json,secrets; from nacl.signing import SigningKey; seed=secrets.token_hex(32); public=SigningKey(bytes.fromhex(seed)).verify_key.encode(); print(seed); print(json.dumps({hashlib.sha256(public).hexdigest()[:32]:public.hex()},separators=(",",":"))); print(hashlib.sha256(public).hexdigest()[:32])')
  export FURIA_GZ_SENSOR_SIGNING_SEED_HEX="${SENSOR_KEY_MATERIAL[0]}"
  export FURIA_SENSOR_VERIFYING_KEYS_JSON="${SENSOR_KEY_MATERIAL[1]}"
  export FURIA_GZ_SENSOR_SIGNING_KEY_ID="${SENSOR_KEY_MATERIAL[2]}"
  unset SENSOR_KEY_MATERIAL
fi
SENSOR_BUILD_SCRIPT="${AGR_SIM:-$HOME/Work/durandal-gz-swarm-sim}/scripts/build_gz_nav_sensor_forwarder.py"
if [[ ! -f "$SENSOR_BUILD_SCRIPT" ]]; then
  printf 'Missing sensor forwarder build script: %s\n' "$SENSOR_BUILD_SCRIPT" >&2
  exit 1
fi
FURIA_GZ_NATS_BRIDGE_TARGET_DIR="$(dirname "$(dirname "$FURIA_GZ_SENSOR_FORWARDER_BIN")")"
export FURIA_GZ_NATS_BRIDGE_TARGET_DIR
if ! uv run --script "$SENSOR_BUILD_SCRIPT" verify >/dev/null; then
  if ! uv run --script "$SENSOR_BUILD_SCRIPT" build >/dev/null; then
    echo "Sensor forwarder build failed; no probe was published." >&2
    exit 2
  fi
fi
if ! uv run --script "$SENSOR_BUILD_SCRIPT" verify >/dev/null; then
  echo "Sensor forwarder receipt verification failed; no probe was published." >&2
  exit 2
fi
SERVER="nats://127.0.0.1:$ANTOINE_GZ_FLIGHT_NATS_PORT"

FLIGHT_PID=""
PROBE_PID=""
FLIGHT_IDENTITY="$REPORT_ROOT/.$RUN_ID-flight.identity"
PROBE_IDENTITY="$REPORT_ROOT/.$RUN_ID-probe.identity"
cleanup() {
  local status=$?
  [[ -z "$PROBE_PID" ]] || dvd_signal_owned_child "$PROJECT_ROOT" "$PROBE_PID" "$PROBE_IDENTITY" TERM || true
  [[ -z "$FLIGHT_PID" ]] || dvd_signal_owned_child "$PROJECT_ROOT" "$FLIGHT_PID" "$FLIGHT_IDENTITY" TERM || true
  [[ -z "$PROBE_PID" ]] || wait "$PROBE_PID" 2>/dev/null || true
  [[ -z "$FLIGHT_PID" ]] || wait "$FLIGHT_PID" 2>/dev/null || true
  rm -f "$PROBE_IDENTITY" "$FLIGHT_IDENTITY"
  return "$status"
}
dvd_install_exit_cleanup_traps cleanup

dvd_run_owned_child "$PROJECT_ROOT" "$FLIGHT_IDENTITY" env DVD_LAUNCHER_MANAGED=1 DVD_LAUNCHER_IDENTITY="$FLIGHT_IDENTITY" "$PROJECT_ROOT/scripts/run-antoine.sh" \
  --run-dir "$RUN_DIR" --flight-seconds 45 --settle-seconds 10 --keep \
  >"$REPORT_ROOT/$RUN_ID-launcher.log" 2>&1 &
FLIGHT_PID=$!
dvd_run_owned_child "$PROJECT_ROOT" "$PROBE_IDENTITY" uv run --project "$PROJECT_ROOT/research" python "$PROJECT_ROOT/research/antoine_gps_tamper.py" \
  --server "$SERVER" --run-dir "$RUN_DIR" --case "$CASE" \
  >"$REPORT_ROOT/$RUN_ID-tampering.log" 2>&1 &
PROBE_PID=$!

set +e
wait "$FLIGHT_PID"
FLIGHT_STATUS=$?
FLIGHT_PID=""
rm -f "$FLIGHT_IDENTITY"
if [[ "$FLIGHT_STATUS" -ne 0 ]] && [[ -n "$PROBE_PID" ]] && kill -0 "$PROBE_PID" 2>/dev/null; then
  dvd_signal_owned_child "$PROJECT_ROOT" "$PROBE_PID" "$PROBE_IDENTITY" TERM || true
fi
wait "$PROBE_PID"
PROBE_STATUS=$?
PROBE_PID=""
rm -f "$PROBE_IDENTITY"
set -e

SUMMARY_STATUS=0
if [[ -f "$RUN_DIR/gz-flight-proof-report.json" && -f "$RUN_DIR/research-gps-tampering.json" ]]; then
  uv run --no-project python "$PROJECT_ROOT/research/summarize_antoine_sensor_tamper.py" \
    "$RUN_DIR" --launcher-exit "$FLIGHT_STATUS" --probe-exit "$PROBE_STATUS" || SUMMARY_STATUS=$?
else
  SUMMARY_STATUS=2
  echo "The run is missing its flight or tampering report; classification remains inconclusive." >&2
fi

printf 'sensor tampering run: %s\n' "$RUN_DIR"
printf 'probe exit: %s\n' "$PROBE_STATUS"
printf 'flight proof exit: %s\n' "$FLIGHT_STATUS"
cat "$REPORT_ROOT/$RUN_ID-tampering.log" 2>/dev/null || true
cat "$REPORT_ROOT/$RUN_ID-launcher.log"
[[ "$PROBE_STATUS" -eq 0 && "$FLIGHT_STATUS" -eq 0 && "$SUMMARY_STATUS" -eq 0 ]]
