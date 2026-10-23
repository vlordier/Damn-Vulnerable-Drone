#!/usr/bin/env bash
# Fly a bounded 2–5 X500 swarm under independent Antoine SITL controllers.
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${PROJECT_ROOT}/research/owned_process.sh"
RUSTUP_TOOLCHAIN="${RUSTUP_TOOLCHAIN:-1.99.1}"
export RUSTUP_TOOLCHAIN
REPORT_ROOT="${ANTOINE_REPORT_DIR:-${PROJECT_ROOT}/build_logs/research/antoine-scenarios}"
RUN_ID="antoine-swarm-flight-$(date -u +%Y%m%dT%H%M%SZ)-$$"
RUN_DIR="${REPORT_ROOT}/${RUN_ID}"
IDENTITY="${REPORT_ROOT}/.${RUN_ID}.identity"
FLIGHT_SECONDS="${ANTOINE_RESEARCH_FLIGHT_SECONDS:-}"
SWARM_BEHAVIOR="${ANTOINE_SWARM_BEHAVIOR:-lanes}"
SWARM_MAVLINK="${ANTOINE_SWARM_MAVLINK:-0}"
MAVLINK_PROBE_PYTHON="${ANTOINE_MAVLINK_PYTHON:-python3.11}"
FLIGHT_PID=""
AUTH_PROBE_PID=""
AUTH_PROBE_STATUS=0
AUTH_PROBE_IDENTITY="${REPORT_ROOT}/.${RUN_ID}-auth-probe.identity"
FAULT="${ANTOINE_SWARM_FAULT:-none}"
FAULT_SECONDS="${ANTOINE_SWARM_FAULT_SECONDS:-4}"
FAULT_STARTUP_TIMEOUT="${ANTOINE_SWARM_FAULT_STARTUP_TIMEOUT:-30}"
FAULT_RATE="${ANTOINE_SWARM_FAULT_RATE_HZ:-10}"
FAULT_MAX_MESSAGES="${ANTOINE_SWARM_FAULT_MAX_MESSAGES:-40}"
FAULT_WAIT_TIMEOUT="${ANTOINE_SWARM_FAULT_WAIT_TIMEOUT_SECONDS:-90}"
AUTH_MUTATION="${ANTOINE_SWARM_AUTH_MUTATION:-wrong-callsign}"
FAULT_STATUS=0
FAULT_PID=""
FAULT_IDENTITY="${REPORT_ROOT}/.${RUN_ID}-fault.identity"
SWARM_SIZE="${ANTOINE_SWARM_SIZE:-2}"
SWARM_SPACING="${ANTOINE_SWARM_SPACING_M:-8}"
ARRIVAL_HOLD_RADIUS="${ANTOINE_GZ_ARRIVAL_HOLD_RADIUS_M:-0.15}"
TERMINAL_BRAKING_RADIUS="${ANTOINE_GZ_TERMINAL_BRAKING_RADIUS_M:-0.15}"
RETAIN_PREDECESSOR_GRANT="${ANTOINE_GZ_RETAIN_PREDECESSOR_GRANT:-1}"
POSITIVE_TARGET="${ANTOINE_SWARM_AUTH_POSITIVE_TARGET:-}"
POSITIVE_DELTA_M="${ANTOINE_SWARM_AUTH_POSITIVE_DELTA_M:-2}"
SWARM_MODELS=(isr fpv-a fpv-b fpv-c fpv-d)
MAVLINK_PIDS=()
MAVLINK_IDENTITIES=()
MAVLINK_PROBE_PID=""
MAVLINK_PROBE_IDENTITY="${REPORT_ROOT}/.${RUN_ID}-mavlink.identity"
MAVLINK_STATUS=0
HOST_CONTEXT_PRE="${REPORT_ROOT}/${RUN_ID}-host-context-pre.json"
HOST_CONTEXT_DURING="${REPORT_ROOT}/${RUN_ID}-host-context-during.jsonl"
HOST_CONTEXT_STOP="${REPORT_ROOT}/.${RUN_ID}-host-context-stop"
HOST_CONTEXT_IDENTITY="${REPORT_ROOT}/.${RUN_ID}-host-context.identity"
HOST_CONTEXT_PID=""
ANTOINE_BRIDGE_DIR="${ANTOINE_BRIDGE_DIR:-${PROJECT_ROOT}/research/antoine-mavlink-jetstream-bridge}"
FLIGHT_FAULT_ENV=()

run_owned_nats_ready() {
  local endpoint="Listening for client connections on 127.0.0.1:${ANTOINE_GZ_FLIGHT_NATS_PORT}"
  [[ -f "${RUN_DIR}/logs/nats.log" ]] &&
    rg -Fq "${endpoint}" "${RUN_DIR}/logs/nats.log" &&
    rg -Fq 'Server is ready' "${RUN_DIR}/logs/nats.log" &&
    nats --no-context --server "nats://127.0.0.1:${ANTOINE_GZ_FLIGHT_NATS_PORT}" \
      rtt >/dev/null 2>&1
}
if [[ ! "${SWARM_SIZE}" =~ ^[0-9]+$ ]] || ((SWARM_SIZE < 2 || SWARM_SIZE > 5)); then
  echo "Swarm size must be an integer from 2 through 5" >&2
  exit 2
fi
if [[ ! "${SWARM_SPACING}" =~ ^[0-9]+([.][0-9]+)?$ ]] || ! awk -v n="${SWARM_SPACING}" 'BEGIN { exit !(n >= 2 && n <= 20) }'; then
  echo "Swarm path spacing must be 2..20 metres" >&2
  exit 2
fi
for radius in "${ARRIVAL_HOLD_RADIUS}" "${TERMINAL_BRAKING_RADIUS}"; do
  if [[ ! "${radius}" =~ ^[0-9]+([.][0-9]+)?$ ]] || ! awk -v n="${radius}" 'BEGIN { exit !(n >= 0.05 && n <= 2) }'; then
    echo "Swarm arrival and braking radii must be 0.05..2 metres" >&2
    exit 2
  fi
done
case "${RETAIN_PREDECESSOR_GRANT}" in
  0 | 1) ;;
  *) echo "ANTOINE_GZ_RETAIN_PREDECESSOR_GRANT must be 0 or 1" >&2; exit 2 ;;
esac
SWARM_AGENTS="${SWARM_MODELS[*]:0:SWARM_SIZE}"
SWARM_AGENTS="${SWARM_AGENTS// /,}"
case "${SWARM_BEHAVIOR}" in
  lanes | column | vee | rendezvous | split) ;;
  *)
    echo "Unsupported swarm behavior: ${SWARM_BEHAVIOR}" >&2
    echo "Choose lanes, column, vee, rendezvous, or split." >&2
    exit 2
    ;;
esac
case "${SWARM_MAVLINK}" in
  0 | 1) ;;
  *)
    echo "ANTOINE_SWARM_MAVLINK must be 0 or 1" >&2
    exit 2
    ;;
esac
if [[ "${SWARM_MAVLINK}" == "1" ]]; then
  uv run --no-project --python "${MAVLINK_PROBE_PYTHON}" python \
    "${PROJECT_ROOT}/research/check_udp_loopback.py"
fi
if [[ -z "${FLIGHT_SECONDS}" ]]; then
  FLIGHT_SECONDS="$(uv run --no-project python "${PROJECT_ROOT}/research/swarm_behavior.py" \
    flight-seconds --behavior "${SWARM_BEHAVIOR}" --size "${SWARM_SIZE}" \
    --spacing "${SWARM_SPACING}" 2>/dev/null || true)"
fi
if [[ -n "${POSITIVE_TARGET}" ]]; then
  if [[ ! "${POSITIVE_TARGET}" =~ ^drone-[1-5]$ ]] || (( ${POSITIVE_TARGET#drone-} > SWARM_SIZE )); then
    echo "Positive-control target must be a callsign in the selected swarm roster." >&2
    exit 2
  fi
  if ! awk -v n="${POSITIVE_DELTA_M}" 'BEGIN { exit !(n >= 1 && n <= 4) }'; then
    echo "Positive-control waypoint delta must be 1..4 metres." >&2
    exit 2
  fi
  FLIGHT_SECONDS="${ANTOINE_RESEARCH_FLIGHT_SECONDS:-100}"
fi
if [[ -z "${FLIGHT_SECONDS}" ]]; then
  echo "Swarm planner did not produce a flight-time budget." >&2
  exit 2
fi
if [[ ! "${FLIGHT_SECONDS}" =~ ^[0-9]+$ ]] || ((FLIGHT_SECONDS < 20 || FLIGHT_SECONDS > 180)); then
  echo "Flight seconds must be an integer from 20 through 180" >&2
  exit 2
fi
# The planner is the single source of truth for waypoint geometry. The same
# script writes the evidence artifact below, so the flight and its evaluation
# cannot drift onto different plans.
LEADER_WAYPOINTS="$(uv run --no-project python "${PROJECT_ROOT}/research/swarm_behavior.py" shell \
  --behavior "${SWARM_BEHAVIOR}" --size "${SWARM_SIZE}" --spacing "${SWARM_SPACING}" | sed -n 1p)"
SECONDARY_WAYPOINTS="$(uv run --no-project python "${PROJECT_ROOT}/research/swarm_behavior.py" shell \
  --behavior "${SWARM_BEHAVIOR}" --size "${SWARM_SIZE}" --spacing "${SWARM_SPACING}" | sed -n 2p)"
if [[ -n "${POSITIVE_TARGET}" ]]; then
  POSITIVE_PLAN_JSON="$(uv run --no-project python - "${PROJECT_ROOT}" "${SWARM_BEHAVIOR}" "${SWARM_SIZE}" "${SWARM_SPACING}" "${SWARM_AGENTS}" "${POSITIVE_TARGET}" "${POSITIVE_DELTA_M}" <<'PY'
import json, sys
from pathlib import Path
root, behavior, size, spacing, models, target, delta = sys.argv[1:]
sys.path.insert(0, str(Path(root) / "research"))
from swarm_behavior import build_plan
plan = build_plan(behavior, int(size), float(spacing), models=models.split(","))
selected = next(item for item in plan["vehicles"] if item["callsign"] == target)
previous = selected["waypointsEnuM"][-1]
command = [round(previous[0] + float(delta), 3), previous[1], previous[2]]
for other in plan["vehicles"]:
    if other["callsign"] != target and min(sum((command[i] - point[i]) ** 2 for i in range(3)) ** 0.5 for point in other["waypointsEnuM"]) < 4:
        raise SystemExit("selected-vehicle command violates 4m planned waypoint clearance")
selected["waypointsEnuM"].append(command)
selected["finalSlotEnuM"] = command
evidence = {"callsign": target, "previousWaypointEnuM": previous, "commandWaypointEnuM": command, "authorizationPath": "normal-run-local-authority-signed-grant"}
plan["selectedVehicleAuthorization"] = evidence
def fmt(points):
    return ";".join(",".join(f"{v:g}" for v in point) for point in points)
print(json.dumps(plan))
PY
  )"
  POSITIVE_PATH_LINES="$(uv run --no-project python -c 'import json,sys; p=json.loads(sys.stdin.read()); f=lambda points:";".join(",".join(f"{v:g}" for v in x) for x in points); print(f(p["vehicles"][0]["waypointsEnuM"])); print("|".join(f(v["waypointsEnuM"]) for v in p["vehicles"][1:]))' <<<"${POSITIVE_PLAN_JSON}")"
  mapfile -t POSITIVE_PATHS <<<"${POSITIVE_PATH_LINES}"
  LEADER_WAYPOINTS="${POSITIVE_PATHS[0]}"
  SECONDARY_WAYPOINTS="${POSITIVE_PATHS[1]}"
  export ANTOINE_GZ_FLIGHT_SWARM_WAYPOINTS="${SECONDARY_WAYPOINTS}"
fi
if [[ -z "${LEADER_WAYPOINTS}" ]] || [[ "${SWARM_SIZE}" -gt 2 && -z "${SECONDARY_WAYPOINTS}" ]]; then
  echo "Swarm planner did not produce the expected per-vehicle paths." >&2
  exit 2
fi
# Check capacity before creating run artifacts or entering the snapshot/build path.
MIN_FREE_MIB="${ANTOINE_SWARM_MIN_FREE_MIB:-4096}" \
  "${PROJECT_ROOT}/research/check-free-space.sh" \
  "${REPORT_ROOT}" "${PROJECT_ROOT}" \
  "${ANTOINE_GZ_CARGO_TARGET_DIR:-${PROJECT_ROOT}/.cargo-target/antoine-gz}"
uv run --project "${PROJECT_ROOT}/research" python \
  "${PROJECT_ROOT}/research/check_swarm_host_load.py"
mkdir -p "${REPORT_ROOT}"
[[ ! -e "${RUN_DIR}" ]] || { echo "Refusing to overwrite evidence: ${RUN_DIR}" >&2; exit 1; }
case "${FAULT}" in none|state-stream-timing|state-stream-partition|controller-state-blackhole|state-blackhole|command-rate|cross-vehicle-authorization) ;; *) echo "Unsupported swarm fault: ${FAULT}" >&2; exit 2 ;; esac
if [[ ! "${FAULT_SECONDS}" =~ ^[0-9]+([.][0-9]+)?$ ]] || ! awk -v n="${FAULT_SECONDS}" 'BEGIN { exit !(n >= 0.25 && n <= 10) }'; then
  echo "Fault duration must be 0.25..10 seconds" >&2
  exit 2
fi
if [[ ! "${FAULT_STARTUP_TIMEOUT}" =~ ^[0-9]+([.][0-9]+)?$ ]] || ! awk -v n="${FAULT_STARTUP_TIMEOUT}" 'BEGIN { exit !(n >= 0.25 && n <= 45) }'; then
  echo "Fault startup timeout must be 0.25..45 seconds" >&2
  exit 2
fi
if [[ ! "${FAULT_RATE}" =~ ^[0-9]+$ ]] || ((FAULT_RATE < 1 || FAULT_RATE > 20)); then
  echo "Fault rate must be 1..20 Hz" >&2
  exit 2
fi
if [[ ! "${FAULT_MAX_MESSAGES}" =~ ^[0-9]+$ ]] || ((FAULT_MAX_MESSAGES < 1 || FAULT_MAX_MESSAGES > 200)); then
  echo "Fault message cap must be 1..200" >&2
  exit 2
fi
if [[ ! "${FAULT_WAIT_TIMEOUT}" =~ ^[0-9]+$ ]] || ((FAULT_WAIT_TIMEOUT < 1 || FAULT_WAIT_TIMEOUT > 300)); then
  echo "Fault pose-gate wait timeout must be 1..300 seconds" >&2
  exit 2
fi
if [[ "${FAULT}" == cross-vehicle-authorization && "${FAULT_MAX_MESSAGES}" != 1 ]]; then
  echo "Cross-vehicle authorization probe publishes exactly one bounded message." >&2
  exit 2
fi
if [[ "${FAULT}" == command-rate ]]; then
  if ! awk -v n="${FAULT_SECONDS}" 'BEGIN { exit !(n <= 5) }' || ((FAULT_MAX_MESSAGES > 100)); then
    echo "Command-rate probe is capped at five seconds and 100 messages." >&2
    exit 2
  fi
  if ! awk -v r="${FAULT_RATE}" -v d="${FAULT_SECONDS}" -v m="${FAULT_MAX_MESSAGES}" \
    'BEGIN { count = int(r * d + 0.999999); exit !(count <= m) }'; then
    echo "Command-rate message cap must cover the configured rate and duration." >&2
    exit 2
  fi
fi
case "${AUTH_MUTATION}" in
  wrong-callsign | wrong-task | wrong-target | stale-run) ;;
  *) echo "Unsupported authorization mutation: ${AUTH_MUTATION}" >&2; exit 2 ;;
esac
FAULT_CALLSIGN="${ANTOINE_SWARM_FAULT_CALLSIGN:-drone-2}"
FAULT_SOURCE="${ANTOINE_SWARM_FAULT_SOURCE:-drone-1}"
if [[ "${FAULT}" != none ]]; then
  if [[ ! "${FAULT_CALLSIGN}" =~ ^drone-[1-5]$ ]]; then
    echo "Fault target must be drone-1..drone-5" >&2
    exit 2
  fi
  fault_index="${FAULT_CALLSIGN#drone-}"
  if ((fault_index < 1 || fault_index > SWARM_SIZE)); then
    echo "Fault target ${FAULT_CALLSIGN} must be inside the selected drone-1..drone-${SWARM_SIZE} roster" >&2
    exit 2
  fi
fi
if [[ "${FAULT}" == command-rate ]]; then
  if [[ ! "${FAULT_SOURCE}" =~ ^drone-[1-5]$ ]] ||
    (( ${FAULT_SOURCE#drone-} < 1 || ${FAULT_SOURCE#drone-} > SWARM_SIZE )) ||
    [[ "${FAULT_SOURCE}" == "${FAULT_CALLSIGN}" ]]; then
    echo "Command-rate source must be a different callsign in the selected roster." >&2
    exit 2
  fi
fi
if [[ "${FAULT}" == state-stream-partition || "${FAULT}" == controller-state-blackhole ]]; then
  blackhole_after_ms="${ANTOINE_SWARM_FAULT_AFTER_MS:-30000}"
  if ! awk -v n="${blackhole_after_ms}" 'BEGIN { exit !(n >= 250 && n <= 120000) }'; then
    echo "State partition start must be 250..120000 ms" >&2
    exit 2
  fi
  blackhole_duration_ms="$(awk -v seconds="${FAULT_SECONDS}" 'BEGIN { printf "%d", seconds * 1000 }')"
  export ANTOINE_GZ_TEST_STATE_BLACKHOLE_CALLSIGN="${FAULT_CALLSIGN}"
  export ANTOINE_GZ_TEST_STATE_BLACKHOLE_AFTER_MS="${blackhole_after_ms}"
  export ANTOINE_GZ_TEST_STATE_BLACKHOLE_DURATION_MS="${blackhole_duration_ms}"
  FLIGHT_FAULT_ENV+=(
    "ANTOINE_GZ_TEST_STATE_BLACKHOLE_CALLSIGN=${FAULT_CALLSIGN}"
    "ANTOINE_GZ_TEST_STATE_BLACKHOLE_AFTER_MS=${blackhole_after_ms}"
    "ANTOINE_GZ_TEST_STATE_BLACKHOLE_DURATION_MS=${blackhole_duration_ms}"
  )
fi
if [[ "${ANTOINE_GZ_SENSOR_FORWARDING:-0}" == "1" || "${ANTOINE_GZ_CAMERA_RENDERING:-0}" == "1" ]]; then
  echo "The two-controller flight runner requires the sensor-forwarding and camera-rendering profiles to be disabled." >&2
  exit 2
fi
ANTOINE_GZ_FLIGHT_GUI="${ANTOINE_GZ_FLIGHT_GUI:-0}"
if [[ "${ANTOINE_GZ_FLIGHT_GUI}" != 0 && "${ANTOINE_GZ_FLIGHT_GUI}" != 1 ]]; then
  echo "ANTOINE_GZ_FLIGHT_GUI must be 0 or 1" >&2
  exit 2
fi
# Pin the swarm baseline to the control rate used by its last qualified run.
# Callers can still override this when measuring another controller rate.
export ANTOINE_GZ_FLIGHT_CONTROL_HZ="${ANTOINE_GZ_FLIGHT_CONTROL_HZ:-20}"
# Capture quaternion, raw/filtered angular rate, torque, and body-up diagnostics
# needed to distinguish control-path instability from delayed or stale state.
export ANTOINE_GZ_RATE_TRACE=1
export ANTOINE_GZ_ARRIVAL_HOLD_RADIUS_M="${ARRIVAL_HOLD_RADIUS}"
export ANTOINE_GZ_TERMINAL_BRAKING_RADIUS_M="${TERMINAL_BRAKING_RADIUS}"
export ANTOINE_GZ_RETAIN_PREDECESSOR_GRANT="${RETAIN_PREDECESSOR_GRANT}"
export ANTOINE_GZ_TWO_DRONE=1
export ANTOINE_GZ_FLIGHT_GUI
export ANTOINE_SWARM_SIZE="${SWARM_SIZE}"
export ANTOINE_SWARM_SPACING_M="${SWARM_SPACING}"
export ANTOINE_SWARM_BEHAVIOR="${SWARM_BEHAVIOR}"
export ANTOINE_SWARM_MAVLINK="${SWARM_MAVLINK}"
export ANTOINE_SWARM_AUTH_POSITIVE_TARGET="${POSITIVE_TARGET}"
export ANTOINE_SWARM_AUTH_POSITIVE_DELTA_M="${POSITIVE_DELTA_M}"
export ANTOINE_GZ_FLIGHT_AGENTS="${SWARM_AGENTS}"
export ANTOINE_GZ_FLIGHT_SWARM_WAYPOINTS="${SECONDARY_WAYPOINTS}"
export ANTOINE_GZ_FLIGHT_GROUND_START=0
ANTOINE_GZ_FLIGHT_NATS_PORT="$(uv run --no-project python -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
[[ "${ANTOINE_GZ_FLIGHT_NATS_PORT}" =~ ^[0-9]+$ ]] || { echo "Invalid run-local NATS port" >&2; exit 2; }
export ANTOINE_GZ_FLIGHT_NATS_PORT
# This runner does not start QGroundControl; an unrelated interactive QGC
# build must not make this Antoine/Gazebo/NATS experiment appear unstable.
export ANTOINE_PROVENANCE_EXTRA_ARGS="--exclude-qgroundcontrol"
cleanup() {
  local status=$? i
  if [[ -n "${HOST_CONTEXT_PID}" ]]; then
    : >"${HOST_CONTEXT_STOP}"
    wait "${HOST_CONTEXT_PID}" 2>/dev/null || true
    HOST_CONTEXT_PID=""
    rm -f "${HOST_CONTEXT_IDENTITY}"
  fi
  rm -f "${HOST_CONTEXT_STOP}"
  # Cleanup failures must not mask the flight runner's original exit status.
  # shellcheck disable=SC2310
  [[ -z "${MAVLINK_PROBE_PID}" ]] || dvd_signal_owned_child "${PROJECT_ROOT}" "${MAVLINK_PROBE_PID}" "${MAVLINK_PROBE_IDENTITY}" TERM || true
  # shellcheck disable=SC2310
  [[ -z "${AUTH_PROBE_PID}" ]] || dvd_signal_owned_child "${PROJECT_ROOT}" "${AUTH_PROBE_PID}" "${AUTH_PROBE_IDENTITY}" TERM || true
  if (( ${#MAVLINK_PIDS[@]} > 0 )); then
    for ((i = 0; i < ${#MAVLINK_PIDS[@]}; i += 1)); do
      # shellcheck disable=SC2310
      dvd_signal_owned_child "${PROJECT_ROOT}" "${MAVLINK_PIDS[${i}]}" "${MAVLINK_IDENTITIES[${i}]}" TERM || true
    done
  fi
  # shellcheck disable=SC2310
  [[ -z "${FAULT_PID}" ]] || dvd_signal_owned_child "${PROJECT_ROOT}" "${FAULT_PID}" "${FAULT_IDENTITY}" TERM || true
  # shellcheck disable=SC2310
  [[ -z "${FLIGHT_PID}" ]] || dvd_signal_owned_child "${PROJECT_ROOT}" "${FLIGHT_PID}" "${IDENTITY}" TERM || true
  [[ -z "${MAVLINK_PROBE_PID}" ]] || wait "${MAVLINK_PROBE_PID}" 2>/dev/null || true
  [[ -z "${AUTH_PROBE_PID}" ]] || wait "${AUTH_PROBE_PID}" 2>/dev/null || true
  if (( ${#MAVLINK_PIDS[@]} > 0 )); then
    for pid in ${MAVLINK_PIDS[@]+"${MAVLINK_PIDS[@]}"}; do
      wait "${pid}" 2>/dev/null || true
    done
  fi
  [[ -z "${FLIGHT_PID}" ]] || wait "${FLIGHT_PID}" 2>/dev/null || true
  [[ -z "${FAULT_PID}" ]] || wait "${FAULT_PID}" 2>/dev/null || true
  rm -f "${IDENTITY}"
  rm -f "${FAULT_IDENTITY}"
  rm -f "${MAVLINK_PROBE_IDENTITY}"
  rm -f "${AUTH_PROBE_IDENTITY}"
  if (( ${#MAVLINK_IDENTITIES[@]} > 0 )); then
    rm -f "${MAVLINK_IDENTITIES[@]}"
  fi
  return "${status}"
}
dvd_install_exit_cleanup_traps cleanup

BRIDGE_BIN=""
if [[ "${SWARM_MAVLINK}" == "1" ]]; then
  if [[ ! -f "${ANTOINE_BRIDGE_DIR}/Cargo.toml" ]]; then
    echo "ANTOINE_BRIDGE_DIR does not contain the MAVLink bridge: ${ANTOINE_BRIDGE_DIR}" >&2
    exit 2
  fi
  BRIDGE_TARGET_DIR="${ANTOINE_BRIDGE_TARGET_DIR:-${PROJECT_ROOT}/.cargo-target/mavlink-bridge}"
  mkdir -p "${BRIDGE_TARGET_DIR}"
  if ! CARGO_TARGET_DIR="${BRIDGE_TARGET_DIR}" RUSTUP_TOOLCHAIN="${RUSTUP_TOOLCHAIN:-1.99.1}" \
    cargo build --locked --manifest-path "${ANTOINE_BRIDGE_DIR}/Cargo.toml" \
    >"${REPORT_ROOT}/${RUN_ID}-bridge-build.log" 2>&1; then
    echo "MAVLink bridge build failed; see ${REPORT_ROOT}/${RUN_ID}-bridge-build.log" >&2
    tail -30 "${REPORT_ROOT}/${RUN_ID}-bridge-build.log" >&2
    exit 2
  fi
  BRIDGE_BIN="${BRIDGE_TARGET_DIR}/debug/antoine-mavlink-jetstream-bridge"
  if [[ ! -x "${BRIDGE_BIN}" ]]; then
    echo "MAVLink bridge build did not produce ${BRIDGE_BIN}" >&2
    exit 2
  fi
fi

uv run --no-project python "${PROJECT_ROOT}/research/capture_swarm_host_context.py" \
  --run-id "${RUN_ID}" --phase pre --project-root "${PROJECT_ROOT}" \
  --output "${HOST_CONTEXT_PRE}"

dvd_run_owned_child "${PROJECT_ROOT}" "${IDENTITY}" env \
  ${FLIGHT_FAULT_ENV[@]+"${FLIGHT_FAULT_ENV[@]}"} \
  "ANTOINE_GZ_ARRIVAL_HOLD_RADIUS_M=${ARRIVAL_HOLD_RADIUS}" \
  "ANTOINE_GZ_TERMINAL_BRAKING_RADIUS_M=${TERMINAL_BRAKING_RADIUS}" \
  "ANTOINE_GZ_RETAIN_PREDECESSOR_GRANT=${RETAIN_PREDECESSOR_GRANT}" \
  DVD_LAUNCHER_MANAGED=1 DVD_LAUNCHER_IDENTITY="${IDENTITY}" \
  "${PROJECT_ROOT}/scripts/run-antoine.sh" \
  --run-dir "${RUN_DIR}" --flight-seconds "${FLIGHT_SECONDS}" --settle-seconds 10 --keep \
  --waypoints "${LEADER_WAYPOINTS}" >"${REPORT_ROOT}/${RUN_ID}-launcher.log" 2>&1 &
FLIGHT_PID=$!
run_dir_ready=0
for _ in {1..300}; do
  if [[ -d "${RUN_DIR}" ]]; then
    run_dir_ready=1
    break
  fi
  sleep 0.2
done
if [[ "${run_dir_ready}" -eq 1 ]]; then
  printf 'swarm run evidence directory: %s\n' "${RUN_DIR}"
  cp "${HOST_CONTEXT_PRE}" "${RUN_DIR}/research-host-context-pre.json"
  dvd_run_owned_child "${PROJECT_ROOT}" "${HOST_CONTEXT_IDENTITY}" \
    uv run --no-project python "${PROJECT_ROOT}/research/capture_swarm_host_context.py" \
    --run-id "${RUN_ID}" --phase during --project-root "${PROJECT_ROOT}" \
    --interval-seconds 5 --stop-file "${HOST_CONTEXT_STOP}" \
    --output "${HOST_CONTEXT_DURING}" \
    >"${REPORT_ROOT}/${RUN_ID}-host-context.log" 2>&1 &
  HOST_CONTEXT_PID=$!
fi
if [[ "${run_dir_ready}" -eq 1 ]]; then
  if [[ -z "${POSITIVE_TARGET}" ]]; then
  uv run --no-project python "${PROJECT_ROOT}/research/swarm_behavior.py" plan \
    --behavior "${SWARM_BEHAVIOR}" --size "${SWARM_SIZE}" --spacing "${SWARM_SPACING}" \
    --models "${SWARM_AGENTS}" --output "${RUN_DIR}/research-swarm-plan.json"
  uv run --no-project python - "${RUN_DIR}" "${SWARM_BEHAVIOR}" "${SWARM_MAVLINK}" "${FAULT}" "${AUTH_MUTATION}" "${FAULT_CALLSIGN}" "${ARRIVAL_HOLD_RADIUS}" "${TERMINAL_BRAKING_RADIUS}" "${RETAIN_PREDECESSOR_GRANT}" <<'PY'
import json
import sys
from pathlib import Path

run_dir, behavior, mavlink, fault, authorization_mutation, fault_callsign, hold_radius, braking_radius, retain_predecessor = sys.argv[1:]
plan = json.loads((Path(run_dir) / "research-swarm-plan.json").read_text(encoding="utf-8"))
config = {
    "contract": "damn-vulnerable-drone.antoine-swarm-config",
    "version": "1.3.0",
    "behavior": behavior,
    "spacingM": plan["spacingM"],
    "mavlinkSwarm": mavlink == "1",
    "fault": fault,
    "authorizationMutation": authorization_mutation,
    "faultCallsign": fault_callsign,
    "arrivalHoldRadiusM": float(hold_radius),
    "terminalBrakingRadiusM": float(braking_radius),
    "retainPredecessorGrant": retain_predecessor == "1",
    "vehicles": [
        {
            "callsign": vehicle["callsign"],
            "model": vehicle["model"],
            "role": vehicle.get("role"),
            "group": vehicle.get("group"),
            "waypointsEnuM": vehicle["waypointsEnuM"],
            "finalSlotEnuM": vehicle["finalSlotEnuM"],
        }
        for vehicle in plan["vehicles"]
    ],
}
(Path(run_dir) / "research-swarm-config.json").write_text(
    json.dumps(config, indent=2) + "\n", encoding="utf-8"
)
PY
  else
    uv run --no-project python - "${RUN_DIR}" "${POSITIVE_PLAN_JSON}" "${SWARM_BEHAVIOR}" "${ARRIVAL_HOLD_RADIUS}" "${TERMINAL_BRAKING_RADIUS}" "${RETAIN_PREDECESSOR_GRANT}" <<'PY'
import json
import sys
from pathlib import Path
run_dir, plan_json, behavior, hold_radius, braking_radius, retain_predecessor = sys.argv[1:]
run_dir = Path(run_dir)
plan = json.loads(plan_json)
(run_dir / "research-swarm-plan.json").write_text(json.dumps(plan, indent=2) + "\n")
evidence = plan["selectedVehicleAuthorization"]
config = {"contract": "damn-vulnerable-drone.antoine-swarm-config", "version": "1.3.0", "behavior": behavior, "spacingM": plan["spacingM"], "mavlinkSwarm": False, "fault": "none", "authorizationMutation": "none", "arrivalHoldRadiusM": float(hold_radius), "terminalBrakingRadiusM": float(braking_radius), "retainPredecessorGrant": retain_predecessor == "1", "selectedVehicleAuthorization": evidence, "vehicles": [{key: vehicle[key] for key in ("callsign", "model", "role", "group", "waypointsEnuM", "finalSlotEnuM") if key in vehicle}]}
(run_dir / "research-swarm-config.json").write_text(json.dumps(config, indent=2) + "\n")
PY
  fi
fi
if [[ "${FAULT}" == cross-vehicle-authorization ]]; then
  broker_ready=0
  for _ in {1..300}; do
    if run_owned_nats_ready; then
      broker_ready=1
      break
    fi
    sleep 0.2
  done
  if [[ "${broker_ready}" != 1 ]]; then
    echo "Run-owned NATS broker did not become ready for the authorization probe." >&2
    exit 2
  fi
  dvd_run_owned_child "${PROJECT_ROOT}" "${AUTH_PROBE_IDENTITY}" \
    uv run --project "${PROJECT_ROOT}/research" python \
    "${PROJECT_ROOT}/research/swarm_cross_vehicle_probe.py" \
    --server "nats://127.0.0.1:${ANTOINE_GZ_FLIGHT_NATS_PORT}" --run-dir "${RUN_DIR}" \
    --source "${ANTOINE_SWARM_FAULT_SOURCE:-drone-1}" \
    --target "${FAULT_CALLSIGN}" --mutation "${AUTH_MUTATION}" --max-messages 1 \
    >"${RUN_DIR}/logs/cross-vehicle-authorization-probe.log" 2>&1 &
  AUTH_PROBE_PID=$!
fi
if [[ "${FAULT}" == command-rate ]]; then
  broker_ready=0
  for _ in {1..300}; do
    if run_owned_nats_ready; then
      broker_ready=1
      break
    fi
    sleep 0.2
  done
  if [[ "${broker_ready}" != 1 ]]; then
    echo "Run-owned NATS broker did not become ready for the bounded rate probe." >&2
    exit 2
  fi
  dvd_run_owned_child "${PROJECT_ROOT}" "${FAULT_IDENTITY}" \
    uv run --project "${PROJECT_ROOT}/research" --group mavlink-check python \
    "${PROJECT_ROOT}/research/antoine_setpoint_rate_probe.py" \
    --server "nats://127.0.0.1:${ANTOINE_GZ_FLIGHT_NATS_PORT}" --run-dir "${RUN_DIR}" \
    --source "${FAULT_SOURCE}" --target "${FAULT_CALLSIGN}" \
    --rate-hz "${FAULT_RATE}" --duration "${FAULT_SECONDS}" \
    --max-messages "${FAULT_MAX_MESSAGES}" --wait-timeout-seconds "${FAULT_WAIT_TIMEOUT}" \
    >"${RUN_DIR}/logs/setpoint-rate-probe.log" 2>&1 &
  FAULT_PID=$!
fi
if [[ "${SWARM_MAVLINK}" == "1" ]]; then
  if [[ "${run_dir_ready}" -ne 1 ]]; then
    echo "Run evidence directory did not become ready; cannot start the MAVLink swarm probe." >&2
    exit 2
  fi
  broker_ready=0
  for _ in {1..300}; do
    run_owned_nats_ready && broker_ready=1
    [[ "${broker_ready}" -eq 1 ]] && break
    sleep 0.2
  done
  if [[ "${broker_ready}" -ne 1 ]]; then
    echo "Run-owned NATS broker did not become ready; cannot exercise the MAVLink swarm bridge." >&2
    exit 2
  fi
  MAVLINK_PORT_OUTPUT="$(uv run --no-project python - "${SWARM_SIZE}" <<'PY'
import socket
import sys

count = int(sys.argv[1])
sockets = []
try:
    for _ in range(count * 2):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sockets.append(sock)
    print(" ".join(str(sock.getsockname()[1]) for sock in sockets))
finally:
    for sock in sockets:
        sock.close()
PY
  )"
  read -r -a MAVLINK_PORTS <<<"${MAVLINK_PORT_OUTPUT}"
  if (( ${#MAVLINK_PORTS[@]} != SWARM_SIZE * 2 )); then
    echo "Could not reserve unique loopback ports for the MAVLink swarm bridges." >&2
    exit 2
  fi
  mkdir -p "${RUN_DIR}/logs"
  MAVLINK_CALLSIGNS=""
  MAVLINK_BIND_PORTS=""
  MAVLINK_PEER_PORTS=""
  MAVLINK_SYSTEM_IDS=""
  for ((index = 0; index < SWARM_SIZE; index += 1)); do
    callsign="drone-$((index + 1))"
    bind_port="${MAVLINK_PORTS[$((index * 2))]}"
    peer_port="${MAVLINK_PORTS[$((index * 2 + 1))]}"
    MAVLINK_CALLSIGNS+="${MAVLINK_CALLSIGNS:+,}${callsign}"
    MAVLINK_BIND_PORTS+="${MAVLINK_BIND_PORTS:+ }${bind_port}"
    MAVLINK_PEER_PORTS+="${MAVLINK_PEER_PORTS:+ }${peer_port}"
    MAVLINK_SYSTEM_IDS+="${MAVLINK_SYSTEM_IDS:+ }$((index + 1))"
    identity="${REPORT_ROOT}/.${RUN_ID}-mavlink-${callsign}.identity"
    dvd_run_owned_child "${PROJECT_ROOT}" "${identity}" env RUST_LOG=info \
      "${BRIDGE_BIN}" --mode antoine-nats --callsign "${callsign}" \
      --nats-url "nats://127.0.0.1:${ANTOINE_GZ_FLIGHT_NATS_PORT}" \
      --qgc-bind "127.0.0.1:${bind_port}" --qgc-peer "127.0.0.1:${peer_port}" \
      --system-id "$((index + 1))" --home-alt-msl 82 \
      >"${RUN_DIR}/logs/mavlink-bridge-${callsign}.log" 2>&1 &
    MAVLINK_PIDS+=($!)
    MAVLINK_IDENTITIES+=("${identity}")
  done
  sleep 1
  if (( ${#MAVLINK_PIDS[@]} > 0 )); then
    for ((index = 0; index < ${#MAVLINK_PIDS[@]}; index += 1)); do
      if ! kill -0 "${MAVLINK_PIDS[${index}]}" 2>/dev/null; then
        echo "MAVLink bridge for drone-$((index + 1)) exited during startup; see ${RUN_DIR}/logs/mavlink-bridge-drone-$((index + 1)).log" >&2
        exit 2
      fi
    done
  fi
  dvd_run_owned_child "${PROJECT_ROOT}" "${MAVLINK_PROBE_IDENTITY}" \
    uv run --no-project --python "${MAVLINK_PROBE_PYTHON}" python \
    "${PROJECT_ROOT}/research/mavlink_swarm_probe.py" \
    --server "nats://127.0.0.1:${ANTOINE_GZ_FLIGHT_NATS_PORT}" \
    --run-dir "${RUN_DIR}" --callsigns "${MAVLINK_CALLSIGNS}" \
    --bind-ports "${MAVLINK_BIND_PORTS}" --peer-ports "${MAVLINK_PEER_PORTS}" \
    --system-ids "${MAVLINK_SYSTEM_IDS}" --duration 45 --max-frames 240 \
    >"${RUN_DIR}/logs/mavlink-swarm-probe.log" 2>&1 &
  MAVLINK_PROBE_PID=$!
fi

if [[ "${FAULT}" != none ]]; then
  case "${FAULT}" in
    state-stream-timing)
      FAULT_SERVER="nats://127.0.0.1:${ANTOINE_GZ_FLIGHT_NATS_PORT}"
      ;;
    cross-vehicle-authorization)
      FAULT_NOTE="one bounded authorization mutation published on the target vehicle's run-owned subject; probe requires explicit controller rejection"
      ;;
    state-stream-partition|controller-state-blackhole)
      FAULT_NOTE="run-local controller-only NATS state-delivery partition; local authority remains subscribed to the source state subject; stale watchdog emits zero-velocity hold before delivery recovery"
      ;;
    state-blackhole)
      FAULT_STATUS=2
      FAULT_NOTE="unsupported: no controller-only blackhole mechanism is configured; no messages injected"
      ;;
    command-rate)
      FAULT_NOTE="capped wrong-callsign setpoint burst to one target subject on the run-owned loopback broker"
      ;;
    *)
      echo "Internal error: validated fault is not handled." >&2
      exit 2
      ;;
  esac
  if [[ "${FAULT}" == state-stream-timing ]]; then
    broker_ready=0
    for _ in {1..300}; do
      nats --no-context --server "${FAULT_SERVER}" rtt >/dev/null 2>&1 && broker_ready=1
      [[ "${broker_ready}" -eq 1 ]] && break
      sleep 0.2
    done
    if [[ "${broker_ready}" -ne 1 || "${run_dir_ready}" -ne 1 ]]; then
      echo "Run-owned broker or evidence directory did not become ready; cannot observe state stream." >&2
      exit 2
    fi
    dvd_run_owned_child "${PROJECT_ROOT}" "${FAULT_IDENTITY}" uv run --project "${PROJECT_ROOT}/research" \
      python "${PROJECT_ROOT}/research/antoine_swarm_fault.py" --server "${FAULT_SERVER}" \
      --run-dir "${RUN_DIR}" --fault "${FAULT}" --duration "${FAULT_SECONDS}" \
      --callsign "${FAULT_CALLSIGN}" \
      --startup-timeout "${FAULT_STARTUP_TIMEOUT}" \
      --rate "${FAULT_RATE}" --max-messages "${FAULT_MAX_MESSAGES}" \
      >"${RUN_DIR}/research-swarm-fault.log" 2>&1 &
    FAULT_PID=$!
  fi
fi
set +e
wait "${FLIGHT_PID}"
FLIGHT_STATUS=$?
set -e
FLIGHT_PID=""
rm -f "${IDENTITY}"
if [[ -n "${FAULT_PID}" ]]; then
  set +e
  wait "${FAULT_PID}"
  FAULT_STATUS=$?
  set -e
  FAULT_PID=""
  rm -f "${FAULT_IDENTITY}"
fi
if [[ -n "${MAVLINK_PROBE_PID}" ]]; then
  set +e
  wait "${MAVLINK_PROBE_PID}"
  MAVLINK_STATUS=$?
  set -e
  MAVLINK_PROBE_PID=""
  rm -f "${MAVLINK_PROBE_IDENTITY}"
fi
if [[ -n "${AUTH_PROBE_PID}" ]]; then
  set +e
  wait "${AUTH_PROBE_PID}"
  AUTH_PROBE_STATUS=$?
  set -e
  AUTH_PROBE_PID=""
  rm -f "${AUTH_PROBE_IDENTITY}"
fi
if [[ "${SWARM_MAVLINK}" == "1" && -f "${RUN_DIR}/research-mavlink-swarm.json" ]]; then
  MAVLINK_SUMMARY_STATUS="$(uv run --project "${PROJECT_ROOT}/research" python - "${RUN_DIR}/research-mavlink-swarm.json" <<'PY'
import json, pathlib, sys
print(json.loads(pathlib.Path(sys.argv[1]).read_text()).get("status", "inconclusive"))
PY
)"
  [[ "${MAVLINK_SUMMARY_STATUS}" == passed ]] || MAVLINK_STATUS=2
fi
if [[ "${FAULT}" == state-stream-timing && -f "${RUN_DIR}/research-swarm-fault.json" ]]; then
  FAULT_SUMMARY_STATUS="$(uv run --project "${PROJECT_ROOT}/research" python - "${RUN_DIR}/research-swarm-fault.json" <<'PY'
import json, pathlib, sys
print(json.loads(pathlib.Path(sys.argv[1]).read_text()).get("status", "inconclusive"))
PY
)"
  [[ "${FAULT_SUMMARY_STATUS}" == observation-only ]] || FAULT_STATUS=2
fi
if [[ "${FAULT:-none}" == state-stream-partition || "${FAULT:-none}" == controller-state-blackhole ]]; then
  set +e
  uv run --project "${PROJECT_ROOT}/research" python \
    "${PROJECT_ROOT}/research/summarize_swarm_state_partition.py" "${RUN_DIR}"
  FAULT_STATUS=$?
  set -e
fi
if [[ "${FAULT:-none}" == state-blackhole ]]; then
  uv run --project "${PROJECT_ROOT}/research" python - "${RUN_DIR}/research-swarm-fault.json" "${RUN_ID}" "${FAULT}" "${FAULT_NOTE}" <<'PY'
import json, pathlib, sys, time
path, run_id, fault, note = sys.argv[1:]
pathlib.Path(path).write_text(json.dumps({
    "contract": "damn-vulnerable-drone.antoine-swarm-fault", "version": "1.0.0",
    "runId": run_id, "fault": fault, "status": "unsupported", "mechanism": note,
    "startedAtUnixNs": time.time_ns(), "completedAtUnixNs": time.time_ns(), "publishedMessages": 0,
    "limits": "No disruption was applied to the actuator/control subject."
}, indent=2) + "\n", encoding="utf-8")
PY
fi
if [[ ! -d "${RUN_DIR}" ]]; then
  cat "${REPORT_ROOT}/${RUN_ID}-launcher.log"
  printf 'two-drone flight did not create a run directory (launcher exit %s)\n' "${FLIGHT_STATUS}" >&2
  exit 1
fi
if [[ -n "${HOST_CONTEXT_PID}" ]]; then
  : >"${HOST_CONTEXT_STOP}"
  set +e
  wait "${HOST_CONTEXT_PID}"
  HOST_CONTEXT_STATUS=$?
  set -e
  HOST_CONTEXT_PID=""
  rm -f "${HOST_CONTEXT_IDENTITY}" "${HOST_CONTEXT_STOP}"
  cp "${HOST_CONTEXT_DURING}" "${RUN_DIR}/research-host-context-during.jsonl" 2>/dev/null || true
  printf 'host context sampler exit: %s\n' "${HOST_CONTEXT_STATUS}"
fi
uv run --no-project python "${PROJECT_ROOT}/research/capture_swarm_host_context.py" \
  --run-id "${RUN_ID}" --phase post --project-root "${PROJECT_ROOT}" \
  --output "${RUN_DIR}/research-host-context-post.json"
set +e
uv run --project "${PROJECT_ROOT}/research" python "${PROJECT_ROOT}/research/summarize_antoine_two_drone_flight.py" "${RUN_DIR}"
SUMMARY_STATUS=$?
set -e
uv run --no-project python "${PROJECT_ROOT}/research/build_evidence_manifest.py" "${RUN_DIR}" >/dev/null
printf 'swarm flight run (%s vehicles): %s\n' "${SWARM_SIZE}" "${RUN_DIR}"
printf 'flight launcher exit: %s\n' "${FLIGHT_STATUS}"
printf 'swarm flight qualification exit: %s\n' "${SUMMARY_STATUS}"
printf 'swarm fault: %s (status %s)\n' "${FAULT}" "${FAULT_STATUS}"
printf 'swarm MAVLink telemetry: %s (status %s)\n' "${SWARM_MAVLINK}" "${MAVLINK_STATUS}"
printf 'cross-vehicle authorization probe: %s (status %s)\n' "${FAULT}" "${AUTH_PROBE_STATUS}"
cat "${REPORT_ROOT}/${RUN_ID}-launcher.log"
if [[ "${SUMMARY_STATUS}" -ne 0 ]]; then exit "${SUMMARY_STATUS}"; fi
if [[ "${FLIGHT_STATUS}" -ne 0 || "${FAULT_STATUS}" -ne 0 ||
  "${MAVLINK_STATUS}" -ne 0 || "${AUTH_PROBE_STATUS}" -ne 0 ]]; then exit 2; fi
