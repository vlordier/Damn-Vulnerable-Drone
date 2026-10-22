#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${PROJECT_ROOT}/research/owned_process.sh"
DVD_PROCESS_SUPERVISOR="$(dvd_process_supervisor "${PROJECT_ROOT}")"
export DVD_PROCESS_SUPERVISOR
ANTOINE_ROOT="${ANTOINE_ROOT:-${HOME}/Work/antoine}"
SIM_ROOT="${AGR_SIM:-${HOME}/Work/durandal-gz-swarm-sim}"
C2_SCRIPTS="${AGR_C2_SCRIPTS:-${HOME}/Work/furia-c2/scripts}"
C2_BRIDGE_ROOT="${AGR_C2_ROOT:-${C2_SCRIPTS%/scripts}}"
RUN_ID="antoine-gz-mavlink-$(date -u +%Y%m%dT%H%M%SZ)-$(uuidgen | tr '[:upper:]' '[:lower:]')"
RUN_DIR="${ANTOINE_GZ_MAVLINK_RUN_DIR:-/tmp/${RUN_ID}}"
mkdir -p "$(dirname "${RUN_DIR}")"
RUN_DIR="$(cd "$(dirname "${RUN_DIR}")" && pwd -P)/$(basename "${RUN_DIR}")"
RUN_ID="$(basename "${RUN_DIR}")"
[[ "${RUN_ID}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || {
  echo "Invalid Antoine/Gazebo run ID derived from run directory: ${RUN_ID}" >&2
  exit 2
}
FLIGHT_RUN_DIR="${RUN_DIR}/flight-proof"
OPEN_QGC="${ANTOINE_GZ_OPEN_QGC:-1}"
HEADLESS_ACCEPTANCE="${ANTOINE_GZ_HEADLESS_ACCEPTANCE:-0}"
loopback_port() {
  uv run --no-project python -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'
}
if [[ "${OPEN_QGC}" == "1" ]]; then
  NATS_DEFAULT=14226 QGC_BIND_DEFAULT=14651 QGC_LISTEN_DEFAULT=14550 GAZEBO_CHECK_DEFAULT=14574
else
  NATS_DEFAULT="$(loopback_port)" QGC_BIND_DEFAULT="$(loopback_port)" \
    QGC_LISTEN_DEFAULT="$(loopback_port)" GAZEBO_CHECK_DEFAULT="$(loopback_port)"
fi
NATS_PORT="${ANTOINE_GZ_MAVLINK_NATS_PORT:-${NATS_DEFAULT}}"
NATS_URL="nats://127.0.0.1:${NATS_PORT}"
BRIDGE_TARGET="${ANTOINE_BRIDGE_TARGET_DIR:-/tmp/dvd-antoine-bridge-target}"
BRIDGE_BIN="${BRIDGE_TARGET}/debug/antoine-mavlink-jetstream-bridge"
MAVLINK_PYTHON="${ANTOINE_MAVLINK_PYTHON:-python3.11}"
MAVLINK_PYTHON_ENV="${ANTOINE_MAVLINK_PYTHON_ENV:-${TMPDIR:-/tmp}/dvd-antoine-mavlink-python311}"
ANTOINE_TARGET="${ANTOINE_GZ_CARGO_TARGET_DIR:-/tmp/antoine-gz-target}"
ANTOINE_RUN_ROOT="${ANTOINE_SEALED_BUILD_ROOT:-}"
ANTOINE_RUN_BUILD_RECEIPT="${ANTOINE_SOURCE_BUILD_RECEIPT:-}"
ANTOINE_RUN_BIN_DIR="${ANTOINE_GZ_PREBUILT_BIN_DIR:-}"
CALLSIGN="${ANTOINE_GZ_MAVLINK_CALLSIGN:-drone-1}"
if [[ ! "${CALLSIGN}" =~ ^[A-Za-z0-9_-]{1,64}$ ]]; then
  echo "Callsign must be 1..64 ASCII letters, digits, '-' or '_' (one NATS subject token)." >&2
  exit 2
fi
QGC_BIND_PORT="${QGC_BRIDGE_PORT:-${QGC_BIND_DEFAULT}}"
QGC_LISTEN_PORT="${QGC_UDP_LISTEN_PORT:-${QGC_LISTEN_DEFAULT}}"
GAZEBO_CHECK_PORT="${MAVLINK_GAZEBO_CHECK_PORT:-${GAZEBO_CHECK_DEFAULT}}"
while [[ "${QGC_BIND_PORT}" == "${QGC_LISTEN_PORT}" || "${QGC_BIND_PORT}" == "${GAZEBO_CHECK_PORT}" || "${QGC_LISTEN_PORT}" == "${GAZEBO_CHECK_PORT}" ]]; do
  QGC_BIND_PORT="$(loopback_port)"
  QGC_LISTEN_PORT="$(loopback_port)"
  GAZEBO_CHECK_PORT="$(loopback_port)"
done
PROFILE_PID=""
BRIDGE_PID=""
GUI_PID=""
PROVENANCE_ENV="${RUN_DIR}/.research-provenance-expected.env"
ANTOINE_SOURCE_SNAPSHOT="${RUN_DIR}/antoine-source-snapshot"
GAZEBO_SOURCE_SNAPSHOT="${RUN_DIR}/gazebo-source-snapshot"
RUNTIME_CONFIG="${RUN_DIR}/interactive-profile.json"
PROFILE_RECEIPT="${RUN_DIR}/interactive-profile-receipt.json"
PROFILE_IDENTITY="${RUN_DIR}/.antoine-profile.identity"
BRIDGE_IDENTITY="${RUN_DIR}/.bridge.identity"
GUI_IDENTITY="${RUN_DIR}/.gazebo-gui.identity"
QGC_SETTINGS_DIR="${QGC_SETTINGS_DIR:-${RUN_DIR}/qgc-settings}"
QGC_SETTINGS_FILE="${QGC_SETTINGS_DIR}/qgroundcontrol.com/QGroundControl Daily.ini"
QGC_RUNTIME_DIR="${QGC_RUNTIME_DIR:-${RUN_DIR}/qgc-runtime}"
export ANTOINE_QGC_DIR="${ANTOINE_QGC_DIR:-${HOME}/Work/antoine-qgroundcontrol}"
export QGC_CONFIG="${QGC_CONFIG:-Debug}"
export QGC_SETTINGS_DIR
[[ "${OPEN_QGC}" == "0" || "${OPEN_QGC}" == "1" ]] || { echo "ANTOINE_GZ_OPEN_QGC must be 0 or 1" >&2; exit 2; }
[[ "${HEADLESS_ACCEPTANCE}" == "0" || "${HEADLESS_ACCEPTANCE}" == "1" ]] || { echo "ANTOINE_GZ_HEADLESS_ACCEPTANCE must be 0 or 1" >&2; exit 2; }
if [[ "${HEADLESS_ACCEPTANCE}" == "1" && "${OPEN_QGC}" != "0" ]]; then
  echo "Headless acceptance requires ANTOINE_GZ_OPEN_QGC=0" >&2
  exit 2
fi
if [[ "${HEADLESS_ACCEPTANCE}" == "1" ]]; then
  UV_PROJECT_ENVIRONMENT="${MAVLINK_PYTHON_ENV}" \
    uv run --locked --project "${PROJECT_ROOT}/research" \
    --python "${MAVLINK_PYTHON}" --group mavlink-check python \
    "${PROJECT_ROOT}/research/check_udp_loopback.py"
fi

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
    --require-input interactive_profile_receipt
  )
  local -a qgc_provenance_args=(--exclude-qgroundcontrol)
  if [[ "${OPEN_QGC}" == "1" ]]; then
    required_inputs+=(--require-input qgroundcontrol_runtime_config --require-input qgroundcontrol_binary)
    qgc_provenance_args=(--qgc-source "${ANTOINE_QGC_DIR}" --qgc-binary "${QGC_BINARY}" --qgc-config "${RUNTIME_CONFIG}")
  fi
  AGR_C2_SCRIPTS="${GAZEBO_SOURCE_SNAPSHOT}/c2-bridge/scripts" \
    ANTOINE_GZ_PREBUILT_BIN_DIR="${ANTOINE_RUN_BIN_DIR}" \
    uv run --no-project python "${PROJECT_ROOT}/research/capture_antoine_provenance.py" \
    "${RUN_DIR}" \
    --phase "${phase}" \
    --source-sim "${SIM_ROOT}" \
    --runtime-world "${VISIBLE_WORLD}" \
    --input-world "${GAZEBO_SOURCE_SNAPSHOT}/worlds/antoine-gz-flight-proof.sdf" \
    --input-applier "${FLIGHT_RUN_DIR}/applier-build/debug/gz-sitl-applier" \
    --input-bridge "${BRIDGE_BIN}" \
    --input-build-receipt "${ANTOINE_RUN_BUILD_RECEIPT}" \
    --input-antoine-source-dir "${ANTOINE_SOURCE_SNAPSHOT}" \
    --input-antoine-snapshot-receipt "${ANTOINE_SOURCE_SNAPSHOT}/antoine-source-snapshot.json" \
    --input-models-dir "${GAZEBO_SOURCE_SNAPSHOT}/models" \
    --input-gazebo-source-dir "${GAZEBO_SOURCE_SNAPSHOT}" \
    --input-snapshot-receipt "${GAZEBO_SOURCE_SNAPSHOT}/gazebo-source-snapshot.json" \
    --antoine-patch "${PROJECT_ROOT}/research/patches/antoine-gazebo-battery-drain.patch" \
    --input-gui-config "${PROJECT_ROOT}/research/gazebo-qgc-gui.config" \
    --input-runtime-config "${RUNTIME_CONFIG}" \
    --profile-receipt "${PROFILE_RECEIPT}" \
    --expected-env "${PROVENANCE_ENV}" \
    "${qgc_provenance_args[@]}" \
    "${required_inputs[@]}"
}

record_process() {
  local name="$1" pid="$2" identity_path="${3:-}"
  shift 3 || true
  local -a identity_args=()
  [[ -z "${identity_path}" ]] || identity_args+=(--supervisor-identity "${identity_path}")
  uv run --no-project python "${PROJECT_ROOT}/research/record_interactive_process.py" \
    "${PROFILE_RECEIPT}" "${name}" "${pid}" "${identity_args[@]}" "$@"
}

supervised_child_pid() {
  local identity_path="$1" child_pid=""
  for _ in $(seq 1 100); do
    child_pid="$(awk -F= '$1 == "child_pid" { print $2; exit }' "${identity_path}" 2>/dev/null || true)"
    [[ -n "${child_pid}" ]] && break
    sleep 0.02
  done
  [[ "${child_pid}" =~ ^[0-9]+$ ]] || return 1
  printf '%s\n' "${child_pid}"
}

cleanup() {
  local status=$?
  [[ -z "${GUI_PID}" ]] || dvd_signal_owned_child "${PROJECT_ROOT}" "${GUI_PID}" "${GUI_IDENTITY}" TERM || true
  [[ -z "${BRIDGE_PID}" ]] || dvd_signal_owned_child "${PROJECT_ROOT}" "${BRIDGE_PID}" "${BRIDGE_IDENTITY}" TERM || true
  [[ -z "${PROFILE_PID}" ]] || dvd_signal_owned_child "${PROJECT_ROOT}" "${PROFILE_PID}" "${PROFILE_IDENTITY}" TERM || true
  [[ -z "${GUI_PID}" ]] || wait "${GUI_PID}" 2>/dev/null || true
  [[ -z "${BRIDGE_PID}" ]] || wait "${BRIDGE_PID}" 2>/dev/null || true
  [[ -z "${PROFILE_PID}" ]] || wait "${PROFILE_PID}" 2>/dev/null || true
  if [[ -f "${PROVENANCE_ENV}" ]]; then
    if ! capture_provenance post; then
      echo "Post-run provenance is incomplete; this run is not provenance-qualified." >&2
    elif [[ ! -f "${RUN_DIR}/research-provenance-pre.json" ]]; then
      echo "Pre-run provenance was never captured; this run is not provenance-qualified." >&2
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
QGC_BINARY="${ANTOINE_QGC_BINARY:-${ANTOINE_QGC_DIR}/build/${QGC_CONFIG}/QGroundControl.app/Contents/MacOS/QGroundControl}"
GZ_BINARY="${GZ_BIN:-${HOME}/Work/gz-macOS/ws/install/bin/gz}"
if [[ "${OPEN_QGC}" == "1" && ! -x "${QGC_BINARY}" ]]; then
  echo "Missing QGroundControl executable for provenance and launch: ${QGC_BINARY}" >&2
  exit 1
fi
if [[ "${OPEN_QGC}" == "1" ]]; then
  uv run --project "${PROJECT_ROOT}/research" python \
    "${PROJECT_ROOT}/research/check_qgroundcontrol_patch.py" \
    --root "${ANTOINE_QGC_DIR}" \
    --patch "${PROJECT_ROOT}/research/patches/qgroundcontrol-antoine.patch" \
    --binary "${QGC_BINARY}"
fi
[[ -x "${HOME}/Work/gz-macOS/ws/install/bin/gz" ]] || {
  echo "Missing source-built Gazebo at ~/Work/gz-macOS/ws/install/bin/gz" >&2
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

mkdir -p "${RUN_DIR}/logs"
echo "Capturing run-owned Antoine and Gazebo rebuild inputs..."
uv run --no-project python "${PROJECT_ROOT}/research/snapshot_antoine_build_inputs.py" \
  "${ANTOINE_ROOT}" "${ANTOINE_ROOT}/../ew-wire-contract" "${ANTOINE_SOURCE_SNAPSHOT}" \
  --sitl-lockfile "${PROJECT_ROOT}/research/locks/antoine-sitl.Cargo.lock" \
  --boundary-lockfile "${PROJECT_ROOT}/research/locks/antoine-boundary.Cargo.lock" \
  --source-patch "${PROJECT_ROOT}/research/patches/antoine-gazebo-battery-drain.patch"
uv run --no-project python "${PROJECT_ROOT}/research/snapshot_gazebo_inputs.py" \
  "${SIM_ROOT}" "${GAZEBO_SOURCE_SNAPSHOT}" --c2-bridge-root "${C2_BRIDGE_ROOT}"
if [[ -n "${ANTOINE_RUN_ROOT}" ]]; then
  [[ -n "${ANTOINE_RUN_BUILD_RECEIPT}" && -n "${ANTOINE_RUN_BIN_DIR}" ]] || {
    echo "A sealed Antoine build root requires its native receipt and binary directory." >&2
    exit 2
  }
  echo "Verifying the prebuilt sealed Antoine snapshot against this run's source bundle..."
  uv run --no-project python "${PROJECT_ROOT}/research/verify_antoine_snapshot_reuse.py" \
    "${ANTOINE_SOURCE_SNAPSHOT}" "$(dirname "${ANTOINE_RUN_ROOT}")"
else
  ANTOINE_RUN_ROOT="${ANTOINE_SOURCE_SNAPSHOT}/antoine"
  ANTOINE_RUN_BUILD_RECEIPT="${ANTOINE_SOURCE_SNAPSHOT}/antoine-source-build-receipt.json"
  ANTOINE_RUN_BIN_DIR="${ANTOINE_RUN_ROOT}/.cargo-target/antoine-sitl-build-receipt/debug"
  echo "Building Antoine from the verified run-owned source snapshot..."
  uv run --no-project python "${PROJECT_ROOT}/research/build_antoine_source_snapshot.py" \
    --snapshot-dir "${ANTOINE_SOURCE_SNAPSHOT}" \
    --target-dir "${RUN_DIR}/antoine-native-target" \
    --rustup-toolchain "${RUSTUP_TOOLCHAIN:-1.99.1}" \
    >"${RUN_DIR}/logs/antoine-snapshot-build.json" 2>"${RUN_DIR}/logs/antoine-snapshot-build.log" || {
      cat "${RUN_DIR}/logs/antoine-snapshot-build.log" >&2
      echo "Sealed Antoine build failed; inspect ${RUN_DIR}/logs/antoine-snapshot-build.log" >&2
      exit 1
    }
fi
uv run --no-project python "${PROJECT_ROOT}/research/verify_antoine_native_receipt.py" \
  --root "${ANTOINE_RUN_ROOT}" \
  --snapshot-root "$(dirname "${ANTOINE_RUN_ROOT}")" \
  --receipt "${ANTOINE_RUN_BUILD_RECEIPT}" \
  --write-shell-environment "${RUN_DIR}/logs/native-build-environment.sh" \
  >"${RUN_DIR}/logs/source-build-receipt-verify.log" 2>&1 || {
    cat "${RUN_DIR}/logs/source-build-receipt-verify.log" >&2
    echo "Sealed Antoine source-build receipt did not verify." >&2
    exit 1
  }
for binary in antoine_sitl_nats antoine_gz_local_authority antoine-boundary-service; do
  [[ -x "${ANTOINE_RUN_BIN_DIR}/${binary}" ]] || {
    echo "Missing source-receipted Antoine binary: ${ANTOINE_RUN_BIN_DIR}/${binary}" >&2
    exit 1
  }
done

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
  printf 'ANTOINE_MAVLINK_PYTHON=%s\n' "${MAVLINK_PYTHON}"
  printf 'ANTOINE_MAVLINK_PYTHON_ENV=%s\n' "${MAVLINK_PYTHON_ENV}"
  printf 'ANTOINE_GZ_OPEN_QGC=%s\n' "${OPEN_QGC}"
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
profile_qgc_args=()
  if [[ "${OPEN_QGC}" == "1" ]]; then
  profile_qgc_args+=(--qgc-root "${ANTOINE_QGC_DIR}" --qgc-binary "${QGC_BINARY}" --qgc-settings "${QGC_SETTINGS_FILE}")
  mkdir -p "$(dirname "${QGC_SETTINGS_FILE}")"
  cat >"${QGC_SETTINGS_FILE}" <<EOF
[AutoConnect]
autoConnectUDP=true
udpListenPort=${QGC_LISTEN_PORT}
udpTargetHostIP=127.0.0.1
udpTargetHostPort=${QGC_BIND_PORT}
EOF
fi
uv run --no-project python "${PROJECT_ROOT}/research/create_interactive_profile_receipt.py" \
  --run-dir "${RUN_DIR}" \
  --antoine-root "${ANTOINE_ROOT}" \
  --sim-root "${SIM_ROOT}" \
  --bridge-binary "${BRIDGE_BIN}" \
  --antoine-nats-binary "${ANTOINE_RUN_BIN_DIR}/antoine_sitl_nats" \
  --antoine-authority-binary "${ANTOINE_RUN_BIN_DIR}/antoine_gz_local_authority" \
  --antoine-boundary-binary "${ANTOINE_RUN_BIN_DIR}/antoine-boundary-service" \
  --gazebo-binary "${GZ_BINARY}" \
  --qgc-patch "${PROJECT_ROOT}/research/patches/qgroundcontrol-antoine.patch" \
  --runtime-world "${VISIBLE_WORLD}" \
  --gui-config "${PROJECT_ROOT}/research/gazebo-qgc-gui.config" \
  --antoine-source-snapshot "${ANTOINE_SOURCE_SNAPSHOT}" \
  --gazebo-source-snapshot "${GAZEBO_SOURCE_SNAPSHOT}" \
  --callsign "${CALLSIGN}" \
  --nats-port "${NATS_PORT}" \
  --qgc-port "${QGC_LISTEN_PORT}" \
  --bridge-port "${QGC_BIND_PORT}" \
  --output "${PROFILE_RECEIPT}" \
  "${profile_qgc_args[@]}"
echo "Starting the isolated Antoine/Gazebo profile at ${NATS_URL}..."
(
  cd "${ANTOINE_RUN_ROOT}"
  source "${RUN_DIR}/logs/native-build-environment.sh"
  dvd_run_owned_child "${PROJECT_ROOT}" "${PROFILE_IDENTITY}" env ANTOINE_GZ_INTERACTIVE_CONTROL=1 \
    ANTOINE_GZ_FLIGHT_RUN_ID="${RUN_ID}" \
    AGR_SIM="${GAZEBO_SOURCE_SNAPSHOT}" \
    AGR_C2_SCRIPTS="${GAZEBO_SOURCE_SNAPSHOT}/c2-bridge/scripts" \
    ANTOINE_GZ_FLIGHT_GUI=0 \
    ANTOINE_GZ_BUILD_DONE=1 ANTOINE_SITL_BUILD_RECEIPT="${ANTOINE_RUN_BUILD_RECEIPT}" \
    ANTOINE_GZ_PREBUILT_BIN_DIR="${ANTOINE_RUN_BIN_DIR}" \
    ANTOINE_GZ_FLIGHT_WORLD_FILE="${VISIBLE_WORLD}" \
    ANTOINE_GZ_INTERACTIVE_TIMEOUT_SECONDS="${ANTOINE_GZ_INTERACTIVE_TIMEOUT_SECONDS:-3600}" \
    ANTOINE_GZ_FLIGHT_NATS_PORT="${NATS_PORT}" \
    ANTOINE_GZ_CARGO_TARGET_DIR="${ANTOINE_TARGET}" \
    GZ_BIN="${GZ_BINARY}" \
    "${ANTOINE_RUN_ROOT}/scripts/gz_flight_proof.sh" \
      --run-dir "${FLIGHT_RUN_DIR}" --keep
) &
PROFILE_PID=$!
PROFILE_CHILD_PID="$(supervised_child_pid "${PROFILE_IDENTITY}")"

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
  "${PROFILE_RECEIPT}" antoineGazeboSupervisor "${PROFILE_CHILD_PID}" \
  --supervisor-identity "${PROFILE_IDENTITY}" \
  --status ready --run-directory "${RUN_DIR}"

echo "Recording pre-interaction source, build, world, and configuration provenance..."
capture_provenance pre

if [[ "${ANTOINE_GZ_FLIGHT_GUI:-1}" == "1" ]]; then
  echo "Starting the framed Gazebo 3D view on the run-owned partition..."
  GZ_INSTALL="$(dirname "$(dirname "${HOME}/Work/gz-macOS/ws/install/bin/gz")")"
  dvd_run_owned_child "${PROJECT_ROOT}" "${GUI_IDENTITY}" env GZ_PARTITION="antoine-gz-flight-${PROFILE_CHILD_PID}" GZ_IP=127.0.0.1 \
    GZ_CLI="${HOME}/Work/gz-macOS/ws/install/bin/gz" \
    GZ_SIM_RESOURCE_PATH="${HOME}/Work/durandal-gz-swarm-sim/models:${HOME}/Work/antoine/models" \
    DYLD_LIBRARY_PATH="${GZ_INSTALL}/lib:${DYLD_LIBRARY_PATH:-}" \
    "${HOME}/Work/gz-macOS/ws/install/bin/gz" sim -g \
    --gui-config "${PROJECT_ROOT}/research/gazebo-qgc-gui.config" \
    >"${RUN_DIR}/gazebo-gui.log" 2>&1 &
  GUI_PID=$!
  GUI_CHILD_PID="$(supervised_child_pid "${GUI_IDENTITY}")"
  sleep 2
  if ! kill -0 "${GUI_PID}" 2>/dev/null; then
    wait "${GUI_PID}" || true
    echo "Gazebo 3D client failed to start; inspect ${RUN_DIR}/gazebo-gui.log" >&2
    exit 1
  fi
  # `gz` may exec through a Ruby launcher; record identity after that handoff
  # so lifecycle checks match the process that actually remains running.
  record_process gazeboGui "${GUI_CHILD_PID}" "${GUI_IDENTITY}"
  echo "Gazebo 3D view: ${RUN_DIR}/gazebo-gui.log"
fi

echo "Starting MAVLink bridge for ${CALLSIGN}; QGC UDP ${QGC_LISTEN_PORT} ↔ ${QGC_BIND_PORT}..."
if lsof -nP -iUDP:"${QGC_BIND_PORT}" >/dev/null 2>&1; then
  echo "UDP ${QGC_BIND_PORT} is already in use; stop its owner before starting this QGC profile." >&2
  exit 1
fi
dvd_run_owned_child "${PROJECT_ROOT}" "${BRIDGE_IDENTITY}" "${BRIDGE_BIN}" --mode gazebo-nats --callsign "${CALLSIGN}" \
  --run-id "${RUN_ID}" \
  --nats-url "${NATS_URL}" \
  --qgc-bind "127.0.0.1:${QGC_BIND_PORT}" \
  --qgc-peer "127.0.0.1:${QGC_LISTEN_PORT}" \
  --qgc-control-peer "127.0.0.1:${GAZEBO_CHECK_PORT}" \
  --qgc-telemetry-peer "127.0.0.1:${GAZEBO_CHECK_PORT}" \
  --system-id 1 --home-alt-msl 82 &
BRIDGE_PID=$!
BRIDGE_CHILD_PID="$(supervised_child_pid "${BRIDGE_IDENTITY}")"
record_process mavlinkBridge "${BRIDGE_CHILD_PID}" "${BRIDGE_IDENTITY}"

bridge_ready=0
for _ in $(seq 1 40); do
  if ! kill -0 "${BRIDGE_PID}" 2>/dev/null; then
    wait "${BRIDGE_PID}" || true
    echo "MAVLink bridge exited during startup. Check whether UDP ${QGC_BIND_PORT} is already in use." >&2
    exit 1
  fi
  bridge_port_pid="$(lsof -tiUDP:"${QGC_BIND_PORT}" 2>/dev/null | tail -1 || true)"
  if [[ "${bridge_port_pid}" == "${BRIDGE_CHILD_PID}" ]]; then
    bridge_ready=1
    break
  fi
  sleep 0.25
done
[[ "${bridge_ready}" == "1" ]] || { echo "Bridge did not bind UDP ${QGC_BIND_PORT}." >&2; exit 1; }

if [[ "${OPEN_QGC}" == "1" ]] && ! lsof -nP -iUDP:"${QGC_LISTEN_PORT}" 2>/dev/null | rg -q 'QGroundCo'; then
  echo "Opening the local QGroundControl build..."
  QGC_UDP_LISTEN_PORT="${QGC_LISTEN_PORT}" QGC_BRIDGE_PORT="${QGC_BIND_PORT}" \
    ANTOINE_QGC_BINARY="${QGC_BINARY}" \
    ANTOINE_QGC_DIR="${ANTOINE_QGC_DIR:-${HOME}/Work/antoine-qgroundcontrol}" \
    QGC_SETTINGS_DIR="${QGC_SETTINGS_DIR}" QGC_SETTINGS_FILE="${QGC_SETTINGS_FILE}" \
    QGC_RUNTIME_DIR="${QGC_RUNTIME_DIR}" \
    QGC_CONFIG="${QGC_CONFIG:-Debug}" "${PROJECT_ROOT}/scripts/run-qgc-antoine.sh"
fi

if [[ "${HEADLESS_ACCEPTANCE}" == "1" ]]; then
  echo "Running bounded headless MAVLink-to-NATS-to-Antoine/Gazebo acceptance..."
  ANTOINE_GZ_FLIGHT_RUN_ID="${RUN_ID}" \
    ANTOINE_GZ_POSE_FILE="${FLIGHT_RUN_DIR}/gazebo-pose.jsonl" \
    ANTOINE_GZ_RUN_DIR="${RUN_DIR}" \
    MAVLINK_NATS_URL="${NATS_URL}" MAVLINK_CALLSIGN="${CALLSIGN}" \
    QGC_BRIDGE_PORT="${QGC_BIND_PORT}" MAVLINK_GAZEBO_CHECK_PORT="${GAZEBO_CHECK_PORT}" \
    UV_PROJECT_ENVIRONMENT="${MAVLINK_PYTHON_ENV}" \
      uv run --locked --project research --python "${MAVLINK_PYTHON}" \
      --group mavlink-check \
      research/mavlink-nats-console/gazebo_flight_check.py
  echo "Headless acceptance passed; stopping this run-owned profile."
  exit 0
fi

if [[ "${OPEN_QGC}" == "1" ]]; then
  echo "Interactive Antoine/Gazebo MAVLink bench is ready."
else
  echo "Headless Antoine/Gazebo MAVLink profile is ready; QGC was not started."
fi
echo "NATS: ${NATS_URL}; callsign: ${CALLSIGN}; MAVLink bridge: 127.0.0.1:${QGC_BIND_PORT}"
echo "Flight evidence: ${FLIGHT_RUN_DIR}"
echo "In another terminal run: make qgc-antoine-check MAVLINK_NATS_URL=${NATS_URL} MAVLINK_CALLSIGN=${CALLSIGN}"
echo "Ctrl-C stops only this run's bridge, Antoine, and Gazebo processes."
wait "${PROFILE_PID}"
