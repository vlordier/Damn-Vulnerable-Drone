#!/usr/bin/env bash
# Run a bounded research scenario against one isolated Antoine/Gazebo/NATS run.
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$PROJECT_ROOT/research/owned_process.sh"
SCENARIO="${1:-}"
if [[ "$SCENARIO" != "waypoint-injection" && "$SCENARIO" != "sequence-poisoning" ]]; then
  echo "Usage: $0 waypoint-injection|sequence-poisoning" >&2
  exit 2
fi

for tool in cargo nats nats-server uv; do
  command -v "$tool" >/dev/null 2>&1 || { echo "Required command not found: $tool" >&2; exit 1; }
done

REPORT_ROOT="${ANTOINE_REPORT_DIR:-$PROJECT_ROOT/build_logs/research/antoine-scenarios}"
mkdir -p "$REPORT_ROOT"
export ANTOINE_REPORT_DIR="$REPORT_ROOT"
RUN_ID="antoine-${SCENARIO}-$(date -u +%Y%m%dT%H%M%SZ)-$$"
RUN_DIR="$REPORT_ROOT/$RUN_ID"
if [[ -e "$RUN_DIR" ]]; then
  echo "Refusing to overwrite research evidence: $RUN_DIR" >&2
  exit 1
fi

if [[ -z "${ANTOINE_GZ_FLIGHT_NATS_PORT:-}" ]]; then
  ANTOINE_GZ_FLIGHT_NATS_PORT="$(uv run --no-project python -c \
    'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
fi
export ANTOINE_GZ_FLIGHT_NATS_PORT
SERVER="nats://127.0.0.1:$ANTOINE_GZ_FLIGHT_NATS_PORT"

PROOF_PID=""
ATTACK_PID=""
PROOF_IDENTITY="$REPORT_ROOT/.$RUN_ID-proof.identity"
ATTACK_IDENTITY="$REPORT_ROOT/.$RUN_ID-attack.identity"
cleanup() {
  local status=$?
  [[ -z "$ATTACK_PID" ]] || dvd_signal_owned_child "$PROJECT_ROOT" "$ATTACK_PID" "$ATTACK_IDENTITY" TERM || true
  [[ -z "$PROOF_PID" ]] || dvd_signal_owned_child "$PROJECT_ROOT" "$PROOF_PID" "$PROOF_IDENTITY" TERM || true
  [[ -z "$ATTACK_PID" ]] || wait "$ATTACK_PID" 2>/dev/null || true
  [[ -z "$PROOF_PID" ]] || wait "$PROOF_PID" 2>/dev/null || true
  rm -f "$ATTACK_IDENTITY" "$PROOF_IDENTITY"
  return "$status"
}
trap cleanup EXIT INT TERM

dvd_run_owned_child "$PROJECT_ROOT" "$PROOF_IDENTITY" env DVD_LAUNCHER_MANAGED=1 DVD_LAUNCHER_IDENTITY="$PROOF_IDENTITY" "$PROJECT_ROOT/scripts/run-antoine.sh" \
  --run-dir "$RUN_DIR" \
  --flight-seconds "${ANTOINE_RESEARCH_FLIGHT_SECONDS:-45}" \
  --settle-seconds 10 \
  --keep >"$REPORT_ROOT/$RUN_ID-launcher.log" 2>&1 &
PROOF_PID=$!

if [[ "$SCENARIO" == "sequence-poisoning" ]]; then
  PROBE_SCRIPT="$PROJECT_ROOT/research/antoine_setpoint_sequence.py"
  SUMMARY_SCRIPT="$PROJECT_ROOT/research/summarize_antoine_sequence.py"
else
  PROBE_SCRIPT="$PROJECT_ROOT/research/antoine_setpoint_mutation.py"
  SUMMARY_SCRIPT="$PROJECT_ROOT/research/summarize_antoine_run.py"
fi
dvd_run_owned_child "$PROJECT_ROOT" "$ATTACK_IDENTITY" uv run --no-project python "$PROBE_SCRIPT" \
  --server "$SERVER" --run-dir "$RUN_DIR" \
  >"$REPORT_ROOT/$RUN_ID-attack.log" 2>&1 &
ATTACK_PID=$!

set +e
wait "$PROOF_PID"
PROOF_STATUS=$?
PROOF_PID=""
rm -f "$PROOF_IDENTITY"
wait "$ATTACK_PID"
ATTACK_STATUS=$?
ATTACK_PID=""
rm -f "$ATTACK_IDENTITY"
set -e

SUMMARY_ARGS=("$RUN_DIR")
if [[ ! -f "$RUN_DIR/research-provenance-comparison.json" ]]; then
  echo "Postflight provenance comparison is missing; scenario qualification will fail closed." >&2
fi
if [[ "$SCENARIO" == "waypoint-injection" && -n "${ANTOINE_BASELINE_REPORT:-}" ]]; then
  SUMMARY_ARGS+=(--baseline "$ANTOINE_BASELINE_REPORT")
fi
set +e
UV_CACHE_DIR="${UV_CACHE_DIR:-/private/tmp/dvd-antoine-uv-cache}" UV_OFFLINE=1 \
  uv run --no-project python "$SUMMARY_SCRIPT" "${SUMMARY_ARGS[@]}"
SUMMARY_STATUS=$?
set -e

printf 'scenario run: %s\n' "$RUN_DIR"
printf 'flight proof exit: %s (2 is expected if the mutation interrupts the mission)\n' "$PROOF_STATUS"
printf 'attack observation exit: %s\n' "$ATTACK_STATUS"
cat "$REPORT_ROOT/$RUN_ID-launcher.log"
cat "$REPORT_ROOT/$RUN_ID-attack.log"

if [[ "$ATTACK_STATUS" -ne 0 ]]; then
  exit "$ATTACK_STATUS"
fi
if [[ "$SUMMARY_STATUS" -ne 0 ]]; then
  exit "$SUMMARY_STATUS"
fi
if [[ "$PROOF_STATUS" -ne 0 && "$PROOF_STATUS" -ne 2 ]]; then
  exit "$PROOF_STATUS"
fi
