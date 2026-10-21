#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ANTOINE_ROOT="${ANTOINE_ROOT:-${HOME}/Work/antoine}"
SIM_ROOT="${AGR_SIM:-${HOME}/Work/durandal-gz-swarm-sim}"
RUN_ID="antoine-gz-mavlink-$(date -u +%Y%m%dT%H%M%SZ)-$(uuidgen | tr '[:upper:]' '[:lower:]')"
RUN_DIR="${ANTOINE_GZ_MAVLINK_RUN_DIR:-/tmp/${RUN_ID}}"
FLIGHT_RUN_DIR="${RUN_DIR}/flight-proof"
NATS_PORT="${ANTOINE_GZ_MAVLINK_NATS_PORT:-14226}"
NATS_URL="nats://127.0.0.1:${NATS_PORT}"
BRIDGE_TARGET="${ANTOINE_BRIDGE_TARGET_DIR:-/tmp/dvd-antoine-bridge-target}"
BRIDGE_BIN="${BRIDGE_TARGET}/debug/antoine-mavlink-jetstream-bridge"
ANTOINE_TARGET="${ANTOINE_GZ_CARGO_TARGET_DIR:-/tmp/antoine-gz-target}"
ANTOINE_BUILD_RECEIPT="${ANTOINE_TARGET}/antoine-gz-battery-build.json"
CALLSIGN="${ANTOINE_GZ_MAVLINK_CALLSIGN:-drone-1}"
QGC_BIND_PORT="${QGC_BRIDGE_PORT:-14651}"
QGC_LISTEN_PORT="${QGC_UDP_LISTEN_PORT:-14550}"
PROFILE_PID=""
BRIDGE_PID=""
GUI_PID=""
PROVENANCE_ENV="${RUN_DIR}/.research-provenance-expected.env"
RUNTIME_CONFIG="${RUN_DIR}/interactive-profile.json"
PROFILE_RECEIPT="${RUN_DIR}/interactive-profile-receipt.json"
QGC_SETTINGS_DIR="${QGC_SETTINGS_DIR:-/tmp/dvd-qgc-antoine-config}"
QGC_SETTINGS_FILE="${QGC_SETTINGS_DIR}/qgroundcontrol.com/QGroundControl Daily.ini"
export ANTOINE_QGC_DIR="${ANTOINE_QGC_DIR:-${HOME}/Work/antoine-qgroundcontrol}"
export QGC_CONFIG="${QGC_CONFIG:-Debug}"
export QGC_SETTINGS_DIR

capture_provenance() {
  local phase="$1"
  local -a required_inputs=(
    --require-input gazebo_binary
    --require-input source_world
    --require-input runtime_world_overlay
    --require-input x500_model
    --require-input research_python_interpreter
    --require-input uv_executable
    --require-input cargo_executable
    --require-input rustc_executable
    --require-input nats-server_executable
    --require-input antoine_flight_proof_launcher
    --require-input antoine_sitl_binary
    --require-input antoine_local_authority_binary
    --require-input antoine_boundary_binary
    --require-input mavlink_nats_bridge_binary
    --require-input antoine_build_receipt
    --require-input antoine_battery_patch
    --require-input gazebo_applier
    --require-input gazebo_gui_config
    --require-input runtime_profile_config
    --require-input qgroundcontrol_runtime_config
    --require-input qgroundcontrol_binary
    --require-input interactive_profile_receipt
  )
  uv run --no-project python "${PROJECT_ROOT}/research/capture_antoine_provenance.py" \
    "${RUN_DIR}" \
    --phase "${phase}" \
    --source-sim "${SIM_ROOT}" \
    --runtime-world "${VISIBLE_WORLD}" \
    --input-world "${SIM_ROOT}/worlds/antoine-gz-flight-proof.sdf" \
    --input-applier "${SIM_ROOT}/tools/gz-sitl-applier/gz-sitl-applier" \
    --input-bridge "${BRIDGE_BIN}" \
    --input-build-receipt "${ANTOINE_BUILD_RECEIPT}" \
    --antoine-patch "${PROJECT_ROOT}/research/patches/antoine-gazebo-battery-drain.patch" \
    --input-gui-config "${PROJECT_ROOT}/research/gazebo-qgc-gui.config" \
    --input-runtime-config "${RUNTIME_CONFIG}" \
    --qgc-source "${ANTOINE_QGC_DIR}" \
    --qgc-config "${RUNTIME_CONFIG}" \
    --profile-receipt "${PROFILE_RECEIPT}" \
    --expected-env "${PROVENANCE_ENV}" \
    "${required_inputs[@]}"
}

stop_child() {
  local child_pid="$1"
  [[ -n "${child_pid}" ]] || return 0
  kill -INT "${child_pid}" 2>/dev/null || true
  for _ in $(seq 1 20); do
    kill -0 "${child_pid}" 2>/dev/null || return 0
    sleep 0.1
  done
  kill -KILL "${child_pid}" 2>/dev/null || true
}

record_process() {
  local name="$1" pid="$2"
  uv run --no-project python "${PROJECT_ROOT}/research/record_interactive_process.py" \
    "${PROFILE_RECEIPT}" "${name}" "${pid}"
}

cleanup() {
  local status=$?
  if [[ -n "${GUI_PID}" ]] && kill -0 "${GUI_PID}" 2>/dev/null; then
    stop_child "${GUI_PID}"
    wait "${GUI_PID}" 2>/dev/null || true
  fi
  if [[ -n "${BRIDGE_PID}" ]] && kill -0 "${BRIDGE_PID}" 2>/dev/null; then
    kill -TERM "${BRIDGE_PID}" 2>/dev/null || true
    wait "${BRIDGE_PID}" 2>/dev/null || true
  fi
  if [[ -n "${PROFILE_PID}" ]] && kill -0 "${PROFILE_PID}" 2>/dev/null; then
    kill -TERM "${PROFILE_PID}" 2>/dev/null || true
    wait "${PROFILE_PID}" 2>/dev/null || true
  fi
  if [[ -f "${PROVENANCE_ENV}" ]]; then
    if ! capture_provenance post; then
      echo "Post-run provenance is incomplete; this run is not provenance-qualified." >&2
    elif ! uv run --no-project python -c \
      'import json, pathlib, sys; result=json.loads(pathlib.Path(sys.argv[1]).read_text()); print("Provenance stable:", result.get("stable")); raise SystemExit(0 if result.get("stable") is True else 1)' \
      "${RUN_DIR}/research-provenance-comparison.json"; then
      echo "Run inputs changed or provenance did not match; inspect ${RUN_DIR}/research-provenance-comparison.json" >&2
    fi
  fi
  echo "Gazebo run files: ${RUN_DIR}"
  return "${status}"
}
trap cleanup EXIT INT TERM

[[ -x "${ANTOINE_ROOT}/scripts/gz_flight_proof.sh" ]] || {
  echo "Missing Antoine Gazebo launcher: ${ANTOINE_ROOT}/scripts/gz_flight_proof.sh" >&2
  exit 1
}
QGC_BINARY="${ANTOINE_QGC_DIR}/build/${QGC_CONFIG}/QGroundControl.app/Contents/MacOS/QGroundControl"
GZ_BINARY="${GZ_BIN:-${HOME}/Work/gz-macOS/ws/install/bin/gz}"
[[ -x "${QGC_BINARY}" ]] || {
  echo "Missing QGroundControl executable for provenance and launch: ${QGC_BINARY}" >&2
  exit 1
}
[[ -x "${HOME}/Work/gz-macOS/ws/install/bin/gz" ]] || {
  echo "Missing source-built Gazebo at ~/Work/gz-macOS/ws/install/bin/gz" >&2
  exit 1
}
[[ -x "${ANTOINE_TARGET}/debug/antoine_sitl_nats" \
  && -x "${ANTOINE_TARGET}/debug/antoine_gz_local_authority" \
  && -x "${ANTOINE_TARGET}/debug/antoine-boundary-service" \
  && -f "${ANTOINE_BUILD_RECEIPT}" ]] || {
  echo "Missing verified Antoine build inputs; run 'make antoine-gz-battery-test' first." >&2
  exit 1
}

VISIBLE_WORLD="${RUN_DIR}.visible.sdf"
if [[ -e "${RUN_DIR}" || -e "${VISIBLE_WORLD}" ]]; then
  echo "Run output already exists; refusing to overwrite evidence: ${RUN_DIR}" >&2
  exit 1
fi

echo "Building the versioned MAVLink/NATS bridge..."
RUSTUP_TOOLCHAIN="${RUSTUP_TOOLCHAIN:-1.99.1}" \
  CARGO_TARGET_DIR="${BRIDGE_TARGET}" cargo build --locked \
  --manifest-path "${PROJECT_ROOT}/research/antoine-mavlink-jetstream-bridge/Cargo.toml"

mkdir -p "$(dirname "${RUN_DIR}")" "${RUN_DIR}"
awk '/    <gravity>/{
  print "    <scene>"
  print "      <ambient>0.75 0.75 0.75 1</ambient>"
  print "      <background>0.72 0.82 0.92 1</background>"
  print "      <grid>true</grid>"
  print "      <shadows>true</shadows>"
  print "    </scene>"
} {
  gsub(/<material><ambient>0.25 0.25 0.25 1<\/ambient><\/material>/,
       "<material><ambient>0.25 0.25 0.25 1</ambient><diffuse>0.42 0.46 0.50 1</diffuse></material>")
  print
}' \
  "${SIM_ROOT}/worlds/antoine-gz-flight-proof.sdf" \
  >"${VISIBLE_WORLD}"
export ANTOINE_GZ_INTERACTIVE_CONTROL=1
export ANTOINE_GZ_FLIGHT_GUI="${ANTOINE_GZ_FLIGHT_GUI:-1}"
export ANTOINE_GZ_FLIGHT_NATS_PORT="${NATS_PORT}"
export ANTOINE_GZ_MAVLINK_CALLSIGN="${CALLSIGN}"
export ANTOINE_GZ_FLIGHT_GROUND_START="${ANTOINE_GZ_FLIGHT_GROUND_START:-1}"
export ANTOINE_GZ_FLIGHT_CONTROL_HZ="${ANTOINE_GZ_FLIGHT_CONTROL_HZ:-25}"
export ANTOINE_GZ_FLIGHT_STATE_HZ="${ANTOINE_GZ_FLIGHT_STATE_HZ:-50}"
{
  printf 'ANTOINE_GZ_INTERACTIVE_CONTROL=%s\n' "${ANTOINE_GZ_INTERACTIVE_CONTROL}"
  printf 'ANTOINE_GZ_FLIGHT_GUI=%s\n' "${ANTOINE_GZ_FLIGHT_GUI}"
  printf 'ANTOINE_GZ_FLIGHT_NATS_PORT=%s\n' "${ANTOINE_GZ_FLIGHT_NATS_PORT}"
  printf 'ANTOINE_GZ_MAVLINK_CALLSIGN=%s\n' "${ANTOINE_GZ_MAVLINK_CALLSIGN}"
  for key in ANTOINE_GZ_SENSOR_FORWARDING ANTOINE_GZ_CAMERA_RENDERING \
    ANTOINE_GZ_FLIGHT_GROUND_START ANTOINE_GZ_FLIGHT_CONTROL_HZ \
    ANTOINE_GZ_FLIGHT_STATE_HZ ANTOINE_GZ_PHYSICS_ENGINE \
    FURIA_GZ_SENSOR_SIGNING_KEY_ID; do
    if [[ -v "${key}" ]]; then
      printf '%s=%s\n' "${key}" "${!key}"
    fi
  done
} >"${PROVENANCE_ENV}"
chmod 600 "${PROVENANCE_ENV}"
mkdir -p "$(dirname "${QGC_SETTINGS_FILE}")"
cat >"${QGC_SETTINGS_FILE}" <<EOF
[AutoConnect]
autoConnectUDP=true
udpListenPort=${QGC_LISTEN_PORT}
udpTargetHostIP=127.0.0.1
udpTargetHostPort=${QGC_BIND_PORT}
EOF
uv run --no-project python "${PROJECT_ROOT}/research/create_interactive_profile_receipt.py" \
  --run-dir "${RUN_DIR}" \
  --antoine-root "${ANTOINE_ROOT}" \
  --sim-root "${SIM_ROOT}" \
  --qgc-root "${ANTOINE_QGC_DIR}" \
  --qgc-binary "${QGC_BINARY}" \
  --bridge-binary "${BRIDGE_BIN}" \
  --antoine-nats-binary "${ANTOINE_TARGET}/debug/antoine_sitl_nats" \
  --antoine-authority-binary "${ANTOINE_TARGET}/debug/antoine_gz_local_authority" \
  --antoine-boundary-binary "${ANTOINE_TARGET}/debug/antoine-boundary-service" \
  --gazebo-binary "${GZ_BINARY}" \
  --qgc-patch "${PROJECT_ROOT}/research/patches/qgroundcontrol-antoine.patch" \
  --runtime-world "${VISIBLE_WORLD}" \
  --qgc-settings "${QGC_SETTINGS_FILE}" \
  --gui-config "${PROJECT_ROOT}/research/gazebo-qgc-gui.config" \
  --callsign "${CALLSIGN}" \
  --nats-port "${NATS_PORT}" \
  --qgc-port "${QGC_LISTEN_PORT}" \
  --bridge-port "${QGC_BIND_PORT}" \
  --output "${PROFILE_RECEIPT}"
echo "Starting the isolated Antoine/Gazebo profile at ${NATS_URL}..."
env ANTOINE_GZ_INTERACTIVE_CONTROL=1 \
  ANTOINE_GZ_FLIGHT_GUI=0 \
  ANTOINE_GZ_FLIGHT_WORLD_FILE="${VISIBLE_WORLD}" \
  ANTOINE_GZ_INTERACTIVE_TIMEOUT_SECONDS="${ANTOINE_GZ_INTERACTIVE_TIMEOUT_SECONDS:-3600}" \
  ANTOINE_GZ_FLIGHT_NATS_PORT="${NATS_PORT}" \
  ANTOINE_GZ_CARGO_TARGET_DIR="${ANTOINE_TARGET}" \
  GZ_BIN="${HOME}/Work/gz-macOS/ws/install/bin/gz" \
  "${ANTOINE_ROOT}/scripts/gz_flight_proof.sh" --run-dir "${FLIGHT_RUN_DIR}" --keep &
PROFILE_PID=$!

ready=0
for _ in $(seq 1 180); do
  if ! kill -0 "${PROFILE_PID}" 2>/dev/null; then
    wait "${PROFILE_PID}" || true
    echo "Antoine/Gazebo profile exited before NATS became ready; inspect ${FLIGHT_RUN_DIR}/logs" >&2
    exit 1
  fi
  if nats --server "${NATS_URL}" rtt >/dev/null 2>&1 \
    && grep -q 'interactive_control=true' "${FLIGHT_RUN_DIR}/logs/local-authority.log" 2>/dev/null \
    && grep -q "ANTOINE_GZ_STATE_RECEIVED callsign=${CALLSIGN}" "${FLIGHT_RUN_DIR}/logs/antoine-controller.log" 2>/dev/null; then
    ready=1
    break
  fi
  sleep 1
done
[[ "${ready}" == "1" ]] || { echo "Timed out waiting for run-owned NATS at ${NATS_URL}" >&2; exit 1; }

uv run --no-project python "${PROJECT_ROOT}/research/record_interactive_process.py" \
  "${PROFILE_RECEIPT}" antoineGazeboSupervisor "${PROFILE_PID}" \
  --status ready --run-directory "${RUN_DIR}"

echo "Recording pre-interaction source, build, world, and configuration provenance..."
capture_provenance pre

if [[ "${ANTOINE_GZ_FLIGHT_GUI:-1}" == "1" ]]; then
  echo "Starting the framed Gazebo 3D view on the run-owned partition..."
  GZ_INSTALL="$(dirname "$(dirname "${HOME}/Work/gz-macOS/ws/install/bin/gz")")"
  env GZ_PARTITION="antoine-gz-flight-${PROFILE_PID}" GZ_IP=127.0.0.1 \
    GZ_CLI="${HOME}/Work/gz-macOS/ws/install/bin/gz" \
    GZ_SIM_RESOURCE_PATH="${HOME}/Work/durandal-gz-swarm-sim/models:${HOME}/Work/antoine/models" \
    DYLD_LIBRARY_PATH="${GZ_INSTALL}/lib:${DYLD_LIBRARY_PATH:-}" \
    "${HOME}/Work/gz-macOS/ws/install/bin/gz" sim -g \
    --gui-config "${PROJECT_ROOT}/research/gazebo-qgc-gui.config" \
    >"${RUN_DIR}/gazebo-gui.log" 2>&1 &
  GUI_PID=$!
  sleep 2
  if ! kill -0 "${GUI_PID}" 2>/dev/null; then
    wait "${GUI_PID}" || true
    echo "Gazebo 3D client failed to start; inspect ${RUN_DIR}/gazebo-gui.log" >&2
    exit 1
  fi
  # `gz` may exec through a Ruby launcher; record identity after that handoff
  # so lifecycle checks match the process that actually remains running.
  record_process gazeboGui "${GUI_PID}"
  echo "Gazebo 3D view: ${RUN_DIR}/gazebo-gui.log"
fi

echo "Starting MAVLink bridge for ${CALLSIGN}; QGC UDP ${QGC_LISTEN_PORT} ↔ ${QGC_BIND_PORT}..."
if lsof -nP -iUDP:"${QGC_BIND_PORT}" >/dev/null 2>&1; then
  echo "UDP ${QGC_BIND_PORT} is already in use; stop its owner before starting this QGC profile." >&2
  exit 1
fi
"${BRIDGE_BIN}" --mode gazebo-nats --callsign "${CALLSIGN}" \
  --nats-url "${NATS_URL}" \
  --qgc-bind "127.0.0.1:${QGC_BIND_PORT}" \
  --qgc-peer "127.0.0.1:${QGC_LISTEN_PORT}" \
  --qgc-telemetry-peer "127.0.0.1:${MAVLINK_GAZEBO_CHECK_PORT:-14574}" \
  --system-id 1 --home-alt-msl 82 &
BRIDGE_PID=$!
record_process mavlinkBridge "${BRIDGE_PID}"

bridge_ready=0
for _ in $(seq 1 40); do
  if ! kill -0 "${BRIDGE_PID}" 2>/dev/null; then
    wait "${BRIDGE_PID}" || true
    echo "MAVLink bridge exited during startup. Check whether UDP ${QGC_BIND_PORT} is already in use." >&2
    exit 1
  fi
  bridge_port_pid="$(lsof -tiUDP:"${QGC_BIND_PORT}" 2>/dev/null | tail -1 || true)"
  if [[ "${bridge_port_pid}" == "${BRIDGE_PID}" ]]; then
    bridge_ready=1
    break
  fi
  sleep 0.25
done
[[ "${bridge_ready}" == "1" ]] || { echo "Bridge did not bind UDP ${QGC_BIND_PORT}." >&2; exit 1; }

if ! lsof -nP -iUDP:"${QGC_LISTEN_PORT}" 2>/dev/null | rg -q 'QGroundCo'; then
  echo "Opening the local QGroundControl build..."
  QGC_UDP_LISTEN_PORT="${QGC_LISTEN_PORT}" QGC_BRIDGE_PORT="${QGC_BIND_PORT}" \
    ANTOINE_QGC_DIR="${ANTOINE_QGC_DIR:-${HOME}/Work/antoine-qgroundcontrol}" \
    QGC_CONFIG="${QGC_CONFIG:-Debug}" "${PROJECT_ROOT}/scripts/run-qgc-antoine.sh"
fi

echo "Interactive Antoine/Gazebo MAVLink bench is ready."
echo "NATS: ${NATS_URL}; callsign: ${CALLSIGN}; QGC target: 127.0.0.1:${QGC_BIND_PORT}"
echo "Flight evidence: ${FLIGHT_RUN_DIR}"
echo "In another terminal run: make qgc-antoine-check MAVLINK_NATS_URL=${NATS_URL} MAVLINK_CALLSIGN=${CALLSIGN}"
echo "Ctrl-C stops only this run's bridge, Antoine, and Gazebo processes."
wait "${PROFILE_PID}"
