#!/usr/bin/env bash
# Run Antoine's native Gazebo/NATS flight proof from this checkout.
# Antoine, Gazebo, and the C2 bridge remain external source checkouts; this
# wrapper only supplies paths and keeps build/evidence output in this project.
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$PROJECT_ROOT/research/owned_process.sh"
# Native receipt builds run before Antoine's launcher can select its fallback.
# Preserve a caller pin; when rustup has no active default, use the installed
# stable channel explicitly so Cargo metadata is produced by a real toolchain.
if ! rustup show active-toolchain >/dev/null 2>&1; then
  export RUSTUP_TOOLCHAIN="${RUSTUP_TOOLCHAIN:-stable}"
fi
RUNTIME_STATE_DIR="$PROJECT_ROOT/.antoine-runtime"
LAUNCHER_IDENTITY="${DVD_LAUNCHER_IDENTITY:-$RUNTIME_STATE_DIR/launcher-$$.identity}"
if [[ "${DVD_LAUNCHER_MANAGED:-0}" != "1" ]]; then
  mkdir -p "$RUNTIME_STATE_DIR"
  supervisor="$(dvd_process_supervisor "$PROJECT_ROOT")"
  exec "$supervisor" run "$LAUNCHER_IDENTITY" -- env \
    DVD_LAUNCHER_MANAGED=1 DVD_LAUNCHER_IDENTITY="$LAUNCHER_IDENTITY" "$0" "$@"
fi
DVD_PROCESS_SUPERVISOR="$(dvd_process_supervisor "$PROJECT_ROOT")"
export DVD_PROCESS_SUPERVISOR
PROOF_ARGS=("$@")
REQUESTED_RUN_DIR=""
ARGS_TO_PARSE=("$@")
while [[ ${#ARGS_TO_PARSE[@]} -gt 0 ]]; do
  case "${ARGS_TO_PARSE[0]}" in
    --run-dir)
      [[ ${#ARGS_TO_PARSE[@]} -gt 1 ]] || { echo "--run-dir requires a path." >&2; exit 2; }
      REQUESTED_RUN_DIR="${ARGS_TO_PARSE[1]}"
      ARGS_TO_PARSE=("${ARGS_TO_PARSE[@]:2}")
      ;;
    *) ARGS_TO_PARSE=("${ARGS_TO_PARSE[@]:1}") ;;
  esac
done
if [[ -n "$REQUESTED_RUN_DIR" && "$REQUESTED_RUN_DIR" != /* ]]; then
  REQUESTED_RUN_DIR="$PWD/$REQUESTED_RUN_DIR"
fi

export ANTOINE_ROOT="${ANTOINE_ROOT:-$HOME/Work/antoine}"
BASE_ANTOINE_ROOT="$ANTOINE_ROOT"
EW_WIRE_SOURCE="${ANTOINE_EW_WIRE_ROOT:-$BASE_ANTOINE_ROOT/../ew-wire-contract}"
SIM_SOURCE="${AGR_SIM:-$HOME/Work/durandal-gz-swarm-sim}"
export AGR_C2_SCRIPTS="${AGR_C2_SCRIPTS:-$HOME/Work/furia-c2/scripts}"
export GZ_BIN="${GZ_BIN:-$HOME/Work/gz-macOS/ws/install/bin/gz}"
export ANTOINE_GZ_PHYSICS_ENGINE="${ANTOINE_GZ_PHYSICS_ENGINE:-gz-physics-bullet-featherstone-plugin}"
export ANTOINE_DVD_RESEARCH_ROOT="$PROJECT_ROOT"
export ANTOINE_GZ_CARGO_TARGET_DIR="${ANTOINE_GZ_CARGO_TARGET_DIR:-$PROJECT_ROOT/.cargo-target/antoine-gz}"
export ANTOINE_REPORT_DIR="${ANTOINE_REPORT_DIR:-$PROJECT_ROOT/build_logs/antoine-gz}"

for required in \
  "$ANTOINE_ROOT/scripts/gz_flight_proof.sh" \
  "$SIM_SOURCE/worlds/antoine-gz-flight-proof.sdf" \
  "$SIM_SOURCE/models/x500/model.sdf" \
  "$SIM_SOURCE/tools/gz-sitl-applier/gz-sitl-applier" \
  "$AGR_C2_SCRIPTS/c2-gz-sitl-bridge.py" \
  "$GZ_BIN"; do
  if [[ ! -e "$required" ]]; then
    printf 'Missing Antoine/Gazebo integration input: %s\n' "$required" >&2
    printf 'Set ANTOINE_ROOT, AGR_SIM, AGR_C2_SCRIPTS, or GZ_BIN to its location.\n' >&2
    exit 1
  fi
done

for tool in cargo nats nats-server uv; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    printf 'Required command not found: %s\n' "$tool" >&2
    exit 1
  fi
done

# Keep one launcher instance per checkout and allow stop.sh to signal only the
# process this wrapper started. The lock path is owned by this checkout.
ACTIVE_RUN_DIR="$RUNTIME_STATE_DIR/active"
if [[ -L "$RUNTIME_STATE_DIR" ]]; then
  echo "Refusing to use a symlink as the Antoine runtime-state directory." >&2
  exit 1
fi
mkdir -p "$RUNTIME_STATE_DIR"
if [[ -L "$ACTIVE_RUN_DIR" ]]; then
  echo "Refusing to use a symlink as the Antoine active-run marker." >&2
  exit 1
fi
if ! mkdir "$ACTIVE_RUN_DIR" 2>/dev/null; then
  ACTIVE_PID="$(cat "$ACTIVE_RUN_DIR/pid" 2>/dev/null || true)"
  if [[ "$ACTIVE_PID" =~ ^[0-9]+$ ]] && kill -0 "$ACTIVE_PID" 2>/dev/null; then
    printf 'An Antoine run is already active in this checkout (PID %s).\n' "$ACTIVE_PID" >&2
    printf 'Use ./stop.sh to stop that run.\n' >&2
    exit 1
  fi
  printf 'Removing stale Antoine run marker (no live PID recorded).\n' >&2
  rm -rf "$ACTIVE_RUN_DIR"
  mkdir "$ACTIVE_RUN_DIR"
fi
printf '%s\n' "$$" > "$ACTIVE_RUN_DIR/pid"
printf '%s\n' "$LAUNCHER_IDENTITY" > "$ACTIVE_RUN_DIR/supervisor-identity"
if [[ ! -f "$LAUNCHER_IDENTITY" || -L "$LAUNCHER_IDENTITY" ]]; then
  echo "Could not verify the native launcher supervisor; refusing to start a flight proof." >&2
  rm -f "$ACTIVE_RUN_DIR/pid" "$ACTIVE_RUN_DIR/supervisor-identity"
  rmdir "$ACTIVE_RUN_DIR" 2>/dev/null || true
  exit 1
fi

RUNTIME_SIMROOT=""
BASE_SIM_SOURCE="${AGR_SIM:-$HOME/Work/durandal-gz-swarm-sim}"
RUNTIME_PROFILE_DIR=""
PROVENANCE_SNAPSHOT_DIR=""
PROVENANCE_GAZEBO_INPUT_ARGS=()
PROVENANCE_BUILD_INPUT_ARGS=()
PROOF_PID=""
PROOF_IDENTITY_PATH=""
RUN_CARGO_TARGET_ROOT=""
RUN_CARGO_TARGET_DIR=""
RUN_BUILD_ID=""
cleanup() {
  local status=$?
  if [[ -n "$PROOF_PID" ]] && kill -0 "$PROOF_PID" 2>/dev/null; then
    if [[ -n "$PROOF_IDENTITY_PATH" ]]; then
      dvd_signal_owned_child "$PROJECT_ROOT" "$PROOF_PID" "$PROOF_IDENTITY_PATH" TERM || \
        echo "Refusing to signal unverified proof child PID $PROOF_PID." >&2
    fi
    wait "$PROOF_PID" 2>/dev/null || true
  fi
  [[ -z "$PROOF_IDENTITY_PATH" ]] || rm -f "$PROOF_IDENTITY_PATH"
  if [[ "$(cat "$ACTIVE_RUN_DIR/pid" 2>/dev/null || true)" == "$$" ]]; then
    rm -f "$ACTIVE_RUN_DIR/pid" "$ACTIVE_RUN_DIR/supervisor-identity"
    rmdir "$ACTIVE_RUN_DIR" 2>/dev/null || true
  fi
  if [[ -n "$RUNTIME_SIMROOT" && "$RUNTIME_SIMROOT" == "$ANTOINE_REPORT_DIR/.runtime-"* ]]; then
    rm -rf "$RUNTIME_SIMROOT"
  fi
  if [[ -n "$RUNTIME_PROFILE_DIR" && "$RUNTIME_PROFILE_DIR" == "$RUNTIME_STATE_DIR/sensor-profile-"* ]]; then
    rm -rf "$RUNTIME_PROFILE_DIR"
  fi
  if [[ -n "$PROVENANCE_SNAPSHOT_DIR" && "$PROVENANCE_SNAPSHOT_DIR" == "$RUNTIME_STATE_DIR/provenance-"* ]]; then
    if ((status == 0)); then
      rm -rf "$PROVENANCE_SNAPSHOT_DIR"
    else
      echo "Preserving failed preflight diagnostics in $PROVENANCE_SNAPSHOT_DIR." >&2
    fi
  fi
  if [[ -n "$RUN_CARGO_TARGET_DIR" && -d "$RUN_CARGO_TARGET_DIR" &&
    ! -L "$RUN_CARGO_TARGET_DIR" &&
    "$RUN_CARGO_TARGET_DIR" == "$RUN_CARGO_TARGET_ROOT/runs/$RUN_BUILD_ID" ]]; then
    rm -rf "$RUN_CARGO_TARGET_DIR"
  fi
  return "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# This macOS Gazebo build has a stale DART dependency. The installed Bullet
# Featherstone engine works with the X500 rotor joints, so select it in an
# isolated run-local SDF overlay and keep the shared simulation checkout intact.
RUNTIME_SIMROOT="$ANTOINE_REPORT_DIR/.runtime-$(date -u +%Y%m%dT%H%M%SZ)-$$"
uv run --no-project python "$PROJECT_ROOT/research/snapshot_gazebo_inputs.py" \
  "$SIM_SOURCE" "$RUNTIME_SIMROOT" \
  --applier-patch "$PROJECT_ROOT/research/patches/gazebo-actuator-lifecycle.patch"
ANTOINE_SOURCE_SNAPSHOT="$RUNTIME_SIMROOT/antoine-source-snapshot"
ANTOINE_RESEARCH_SOURCE_PATCH="$RUNTIME_SIMROOT/antoine-research-source.patch"
cat "$PROJECT_ROOT/research/patches/antoine-flight-lifecycle.patch" \
  "$PROJECT_ROOT/research/patches/antoine-swarm-state-partition.patch" \
  >"$ANTOINE_RESEARCH_SOURCE_PATCH"
uv run --no-project python "$PROJECT_ROOT/research/snapshot_antoine_build_inputs.py" \
  "$BASE_ANTOINE_ROOT" "$EW_WIRE_SOURCE" "$ANTOINE_SOURCE_SNAPSHOT" \
  --sitl-lockfile "$PROJECT_ROOT/research/locks/antoine-sitl.Cargo.lock" \
  --boundary-lockfile "$PROJECT_ROOT/research/locks/antoine-boundary.Cargo.lock" \
  --source-patch "$ANTOINE_RESEARCH_SOURCE_PATCH"
RUNTIME_ANTOINE_ROOT="$ANTOINE_SOURCE_SNAPSHOT/antoine"
RUN_CARGO_TARGET_ROOT="${ANTOINE_GZ_CARGO_TARGET_DIR:-$PROJECT_ROOT/.cargo-target/antoine-gz}"
RUN_BUILD_ID="${REQUESTED_RUN_DIR##*/}"
[[ -n "$RUN_BUILD_ID" ]] || RUN_BUILD_ID="run-$$"
RUN_CARGO_TARGET_DIR="$RUN_CARGO_TARGET_ROOT/runs/$RUN_BUILD_ID"
if [[ -e "$RUN_CARGO_TARGET_DIR" || -L "$RUN_CARGO_TARGET_DIR" ]]; then
  echo "Refusing to reuse run-owned Cargo target: $RUN_CARGO_TARGET_DIR" >&2
  exit 1
fi
mkdir -p "$RUN_CARGO_TARGET_DIR"
export ANTOINE_GZ_CARGO_TARGET_DIR="$RUN_CARGO_TARGET_DIR"
export ANTOINE_GZ_PREBUILT_BIN_DIR="$RUNTIME_SIMROOT/bin"
export ANTOINE_GZ_BUILD_DONE=1
mkdir -p "$RUNTIME_SIMROOT/runtime-worlds"
sed 's#<plugin filename="gz-sim-physics-system" name="gz::sim::systems::Physics"/>#<plugin filename="gz-sim-physics-system" name="gz::sim::systems::Physics"><engine><filename>gz-physics-bullet-featherstone-plugin</filename></engine></plugin>#' \
  "$RUNTIME_SIMROOT/worlds/antoine-gz-flight-proof.sdf" \
  > "$RUNTIME_SIMROOT/runtime-worlds/antoine-gz-flight-proof.sdf"
if [[ "${ANTOINE_SWARM_AUTH_POSITIVE_TARGET:-}" != "" ]]; then
  # The controller authority loop is wall-clock scheduled while Gazebo publishes
  # simulation timestamps. Under recorder load, factor 1.0 can let world time
  # outrun the authority's sampled state; slow only this opt-in experiment so
  # the independently measured clocks remain correlatable.
  sed -i.bak 's#<real_time_factor>1.0</real_time_factor>#<real_time_factor>0.5</real_time_factor>#' \
    "$RUNTIME_SIMROOT/runtime-worlds/antoine-gz-flight-proof.sdf"
  rm -f "$RUNTIME_SIMROOT/runtime-worlds/antoine-gz-flight-proof.sdf.bak"
fi
if ! rg -q '<filename>gz-physics-bullet-featherstone-plugin</filename>' \
  "$RUNTIME_SIMROOT/runtime-worlds/antoine-gz-flight-proof.sdf"; then
  echo "Could not select the installed Bullet Featherstone physics engine in the run-local world." >&2
  exit 1
fi
if [[ "${ANTOINE_GZ_TWO_DRONE:-0}" == "1" || -n "${ANTOINE_GZ_FLIGHT_AGENTS:-}" ]]; then
  uv run --no-project python "$PROJECT_ROOT/research/prepare_antoine_two_drone_world.py" \
    "$RUNTIME_SIMROOT/runtime-worlds/antoine-gz-flight-proof.sdf" \
    "$RUNTIME_SIMROOT/runtime-worlds/antoine-gz-flight-proof-swarm.sdf" \
    --agents "${ANTOINE_GZ_FLIGHT_AGENTS:-isr,fpv-a}" \
    --spacing-m "${ANTOINE_SWARM_SPACING_M:-8}" >/dev/null
  mv "$RUNTIME_SIMROOT/runtime-worlds/antoine-gz-flight-proof-swarm.sdf" \
    "$RUNTIME_SIMROOT/runtime-worlds/antoine-gz-flight-proof.sdf"
  export ANTOINE_GZ_FLIGHT_MULTI_DRONE=1
fi
if [[ "${ANTOINE_GZ_SENSOR_FORWARDING:-0}" == "1" ]]; then
  WORLD_OVERLAY="$RUNTIME_SIMROOT/runtime-worlds/antoine-gz-flight-proof.sdf"
  if [[ "$(rg -c '<plugin filename="gz-sim-scene-broadcaster-system"' "$WORLD_OVERLAY" || true)" != "1" ]]; then
    echo "Could not identify the unique scene-broadcaster insertion point for sensor systems." >&2
    exit 1
  fi
  awk '
    { print }
    /<plugin filename="gz-sim-scene-broadcaster-system"/ {
      print "    <spherical_coordinates>"
      print "      <surface_model>EARTH_WGS84</surface_model>"
      print "      <world_frame_orientation>ENU</world_frame_orientation>"
      print "      <latitude_deg>48.0002</latitude_deg>"
      print "      <longitude_deg>0.3564</longitude_deg>"
      print "      <elevation>82</elevation>"
      print "    </spherical_coordinates>"
      print "    <plugin filename=\"gz-sim-imu-system\" name=\"gz::sim::systems::Imu\"/>"
      print "    <plugin filename=\"gz-sim-navsat-system\" name=\"gz::sim::systems::NavSat\"/>"
      print "    <plugin filename=\"gz-sim-air-pressure-system\" name=\"gz::sim::systems::AirPressure\"/>"
      if (ENVIRON["ANTOINE_GZ_CAMERA_RENDERING"] == "1")
        print "    <plugin filename=\"gz-sim-sensors-system\" name=\"gz::sim::systems::Sensors\"><render_engine>ogre2</render_engine></plugin>"
    }
  ' "$WORLD_OVERLAY" >"$WORLD_OVERLAY.tmp"
  mv "$WORLD_OVERLAY.tmp" "$WORLD_OVERLAY"
  for plugin in gz-sim-imu-system gz-sim-navsat-system gz-sim-air-pressure-system; do
    rg -q "filename=\"$plugin\"" "$WORLD_OVERLAY" || {
      echo "Failed to enable required local Gazebo sensor system: $plugin" >&2
      exit 1
    }
  done
  if [[ "${ANTOINE_GZ_CAMERA_RENDERING:-0}" == "1" ]]; then
    rg -q 'filename="gz-sim-sensors-system"' "$WORLD_OVERLAY" || {
      echo "Failed to enable the local Gazebo camera sensor system." >&2
      exit 1
    }
  fi
fi
export AGR_SIM="$RUNTIME_SIMROOT"
export ANTOINE_GZ_FLIGHT_WORLD_FILE="$RUNTIME_SIMROOT/runtime-worlds/antoine-gz-flight-proof.sdf"

# Antoine starts Gazebo in a background job after sourcing its own environment.
# Its gz-macOS colcon install does not export runtime search paths from setup.bash,
# so make the built system plugins and their dependent dylibs discoverable.
GZ_INSTALL="$(cd "$(dirname "$GZ_BIN")/.." && pwd)"
if [[ -f "$GZ_INSTALL/setup.bash" ]]; then
  set +u
  # shellcheck source=/dev/null
  source "$GZ_INSTALL/setup.bash" >/dev/null 2>&1 || true
  set -u
fi
export GZ_SIM_SYSTEM_PLUGIN_PATH="$GZ_INSTALL/lib${GZ_SIM_SYSTEM_PLUGIN_PATH:+:$GZ_SIM_SYSTEM_PLUGIN_PATH}"
export DYLD_LIBRARY_PATH="$GZ_INSTALL/lib${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"
if [[ "${ANTOINE_GZ_CAMERA_RENDERING:-0}" == "1" ]]; then
  RENDERING_PREFIX="${FURIA_GZ_RENDERING_PREFIX:-$PROJECT_ROOT/runs/gz-rendering-ogre2-prefix}"
  RENDER_ENGINE_DIR="${FURIA_GZ_RENDER_ENGINE_PATH:-$RENDERING_PREFIX/lib/gz-rendering-8/engine-plugins}"
  RENDERING_CORE="$RENDERING_PREFIX/lib/libgz-rendering8.8.dylib"
  RENDERING_SENSOR_OVERLAY="$RENDERING_PREFIX/lib/libgz-sim8-sensors-system.dylib"
  if [[ ! -f "$RENDERING_CORE" || ! -f "$RENDERING_SENSOR_OVERLAY" || ! -d "$RENDER_ENGINE_DIR" ]]; then
    printf 'The local Ogre2 rendering runtime is incomplete under %s.\n' "$RENDERING_PREFIX" >&2
    exit 1
  fi
  export FURIA_GZ_RENDER_ENGINE_PATH="$RENDER_ENGINE_DIR"
  export GZ_SIM_RENDER_ENGINE_PATH="$RENDER_ENGINE_DIR"
  export GZ_RENDERING_PLUGIN_PATH="$RENDER_ENGINE_DIR"
  export GZ_SIM_SYSTEM_PLUGIN_PATH="$RENDERING_PREFIX/lib:$GZ_SIM_SYSTEM_PLUGIN_PATH"
  export DYLD_LIBRARY_PATH="$RENDERING_PREFIX/lib:$DYLD_LIBRARY_PATH"
  export DYLD_INSERT_LIBRARIES="$RENDERING_CORE${DYLD_INSERT_LIBRARIES:+:$DYLD_INSERT_LIBRARIES}"
fi

# Antoine's controller has no ground takeoff mode, so use the proof's documented
# airborne start unless the operator explicitly requests ground-start testing.
export ANTOINE_GZ_FLIGHT_GROUND_START="${ANTOINE_GZ_FLIGHT_GROUND_START:-0}"

# Antoine's upstream proof launcher defaults to port 14222 and only checks
# whether *some* NATS server answers there after it starts its own server. That
# can silently attach a run to another demo's bus. Reserve a fresh loopback port
# number for this run unless the operator explicitly pins one.
PROOF_SCRIPT="$RUNTIME_ANTOINE_ROOT/scripts/gz_flight_proof.sh"
if [[ "${ANTOINE_GZ_SENSOR_FORWARDING:-0}" == "1" ]]; then
  export FURIA_GZ_NATS_BRIDGE_TARGET_DIR="${FURIA_GZ_NATS_BRIDGE_TARGET_DIR:-$SIM_SOURCE/target/gz-nats-bridge}"
  FURIA_GZ_SENSOR_FORWARDER_BIN="${FURIA_GZ_SENSOR_FORWARDER_BIN:-$FURIA_GZ_NATS_BRIDGE_TARGET_DIR/debug/gz-nav-sensor-forwarder}"
  SENSOR_BUILD_SCRIPT="$SIM_SOURCE/scripts/build_gz_nav_sensor_forwarder.py"
  if [[ ! -x "$FURIA_GZ_SENSOR_FORWARDER_BIN" || ! -f "${FURIA_GZ_SENSOR_FORWARDER_BIN}.build.json" || ! -f "$SENSOR_BUILD_SCRIPT" ]]; then
    echo "A source-bound gz-nav-sensor-forwarder build and receipt are required for this scenario." >&2
    exit 1
  fi
  uv run --script "$SENSOR_BUILD_SCRIPT" verify >/dev/null
  RUNTIME_PROFILE_DIR="$RUNTIME_STATE_DIR/sensor-profile-$(date -u +%Y%m%dT%H%M%SZ)-$$"
  if [[ -e "$RUNTIME_PROFILE_DIR" || -L "$RUNTIME_PROFILE_DIR" ]]; then
    echo "Refusing to overwrite sensor-profile overlay: $RUNTIME_PROFILE_DIR" >&2
    exit 1
  fi
  mkdir "$RUNTIME_PROFILE_DIR"
  PROOF_SCRIPT="$RUNTIME_PROFILE_DIR/gz_flight_proof.sh"
  export ANTOINE_GZ_SENSOR_PROFILE_LAUNCHER="$PROOF_SCRIPT"
  uv run --no-project python "$PROJECT_ROOT/research/prepare_antoine_sensor_profile.py" \
    "$RUNTIME_ANTOINE_ROOT/scripts/gz_flight_proof.sh" "$PROOF_SCRIPT" "$RUNTIME_ANTOINE_ROOT" >/dev/null
  export ANTOINE_GZ_SENSOR_PROFILE_JSON="$RUNTIME_PROFILE_DIR/profile.json"
  export FURIA_GZ_SENSOR_FORWARDER_BIN
  if [[ -z "${FURIA_GZ_SENSOR_SIGNING_SEED_HEX:-}" ]]; then
    FURIA_GZ_SENSOR_SIGNING_SEED_HEX="$(uv run --no-project --with pynacl python -c 'import secrets; print(secrets.token_hex(32))')"
    export FURIA_GZ_SENSOR_SIGNING_SEED_HEX
  fi
  if [[ -z "${FURIA_SENSOR_VERIFYING_KEYS_JSON:-}" ]]; then
    SENSOR_VERIFYING_KEYS="$(uv run --no-project --with pynacl python -c 'import hashlib,json,os; from nacl.signing import SigningKey; public=SigningKey(bytes.fromhex(os.environ["FURIA_GZ_SENSOR_SIGNING_SEED_HEX"])).verify_key.encode(); print(json.dumps({hashlib.sha256(public).hexdigest()[:32]:public.hex()},separators=(",",":")))')"
    export FURIA_SENSOR_VERIFYING_KEYS_JSON="$SENSOR_VERIFYING_KEYS"
    unset SENSOR_VERIFYING_KEYS
  fi
  FURIA_GZ_SENSOR_SIGNING_KEY_ID="$(uv run --no-project python -c 'import json,os; print(",".join(sorted(json.loads(os.environ["FURIA_SENSOR_VERIFYING_KEYS_JSON"]))))')"
  export FURIA_GZ_SENSOR_SIGNING_KEY_ID
  if [[ -z "${FURIA_GZ_SENSOR_EXPECTED_GPS_FRAMES_JSON:-}" ]]; then
    export FURIA_GZ_SENSOR_EXPECTED_GPS_FRAMES_JSON='{"drone-1":"isr::base_link::gps"}'
  fi
fi

if [[ -z "${ANTOINE_GZ_FLIGHT_NATS_PORT:-}" ]]; then
  if ! ANTOINE_GZ_FLIGHT_NATS_PORT="$(uv run --no-project python -c \
    'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"; then
    echo "Could not reserve a free local NATS port." >&2
    exit 1
  fi
  [[ "$ANTOINE_GZ_FLIGHT_NATS_PORT" =~ ^[0-9]+$ ]] || {
    echo "Port selection returned an invalid NATS port." >&2
    exit 1
  }
  export ANTOINE_GZ_FLIGHT_NATS_PORT
fi

write_public_profile_env() {
  local key value
  for key in \
    ANTOINE_GZ_CAMERA_RENDERING \
    ANTOINE_GZ_FLIGHT_CONTROL_HZ \
    ANTOINE_GZ_TORQUE_GAIN \
    ANTOINE_GZ_ATTITUDE_RATE_GAIN \
    ANTOINE_GZ_FLIGHT_GUI \
    ANTOINE_GZ_FLIGHT_DRONE_2_WAYPOINTS \
    ANTOINE_GZ_FLIGHT_AGENTS \
    ANTOINE_GZ_FLIGHT_SWARM_WAYPOINTS \
    ANTOINE_SWARM_SIZE \
    ANTOINE_SWARM_SPACING_M \
    ANTOINE_SWARM_BEHAVIOR \
    ANTOINE_SWARM_MAVLINK \
    ANTOINE_GZ_FLIGHT_GROUND_START \
    ANTOINE_GZ_FLIGHT_NATS_PORT \
    ANTOINE_GZ_FLIGHT_STATE_HZ \
    ANTOINE_GZ_FLIGHT_MULTI_DRONE \
    ANTOINE_GZ_PHYSICS_ENGINE \
    ANTOINE_GZ_SENSOR_FORWARDING \
    ANTOINE_GZ_TWO_DRONE \
    FURIA_GZ_SENSOR_SIGNING_KEY_ID; do
    if value="$(printenv "$key")"; then
      printf '%s=%s\n' "$key" "$value"
    fi
  done | LC_ALL=C sort
}

# Only bind the sensor forwarder and its build receipt when this run actually
# loads the sensor profile. Hashing an unrelated artifact made ordinary flight
# baselines appear unstable during a concurrent forwarder rebuild.
PROVENANCE_SENSOR_INPUT_ARGS=()
if [[ "${ANTOINE_GZ_SENSOR_FORWARDING:-0}" == "1" ]]; then
  PROVENANCE_SENSOR_INPUT_ARGS+=(
    --input-forwarder "${FURIA_GZ_SENSOR_FORWARDER_BIN:-$BASE_SIM_SOURCE/target/gz-nats-bridge/debug/gz-nav-sensor-forwarder}"
    --input-forwarder-build "$BASE_SIM_SOURCE/scripts/build_gz_nav_sensor_forwarder.py"
  )
fi

# Antoine creates the run directory atomically before any flight build or
# Gazebo launch. Snapshot inputs now, then attach the snapshot to that owned
# directory as soon as it exists.
if [[ -n "$REQUESTED_RUN_DIR" ]]; then
  PROVENANCE_GAZEBO_INPUT_ARGS=(
    --input-world "$RUNTIME_SIMROOT/worlds/antoine-gz-flight-proof.sdf"
    --input-x500-model "$RUNTIME_SIMROOT/models/x500/model.sdf"
    --input-models-dir "$RUNTIME_SIMROOT/models"
    --input-applier "$RUNTIME_SIMROOT/tools/gz-sitl-applier/gz-sitl-applier"
    --input-snapshot-receipt "$RUNTIME_SIMROOT/gazebo-source-snapshot.json"
  )
  PROVENANCE_BUILD_INPUT_ARGS=(
    --input-build-receipt "$RUNTIME_SIMROOT/antoine-build-receipt.json"
    --input-antoine-source-dir "$ANTOINE_SOURCE_SNAPSHOT"
    --input-antoine-snapshot-receipt "$ANTOINE_SOURCE_SNAPSHOT/antoine-source-snapshot.json"
  )
  PROVENANCE_SNAPSHOT_DIR="$RUNTIME_STATE_DIR/provenance-$$"
  mkdir -m 700 "$PROVENANCE_SNAPSHOT_DIR"
  RUN_ID_FOR_PROVENANCE="${REQUESTED_RUN_DIR##*/}"
  ANTOINE_BUILD_LOG="$PROVENANCE_SNAPSHOT_DIR/antoine-preflight-build.log"
  NATIVE_BUILD_RECEIPT_ARGS=()
  if [[ -f "$RUNTIME_ANTOINE_ROOT/scripts/antoine_sitl_build_receipt.py" ]]; then
    ANTOINE_SEALED_TARGET="$RUN_CARGO_TARGET_ROOT/sealed-builds/$RUN_BUILD_ID"
    if [[ -e "$ANTOINE_SEALED_TARGET" || -L "$ANTOINE_SEALED_TARGET" ]]; then
      echo "Refusing to reuse sealed Antoine build target: $ANTOINE_SEALED_TARGET" >&2
      exit 1
    fi
    if ! uv run --no-project python "$PROJECT_ROOT/research/build_antoine_source_snapshot.py" \
        --snapshot-dir "$ANTOINE_SOURCE_SNAPSHOT" \
        --target-dir "$ANTOINE_SEALED_TARGET" \
        --rustup-toolchain "$RUSTUP_TOOLCHAIN" >"$ANTOINE_BUILD_LOG" 2>&1; then
      echo "Antoine native source-bound build failed; see $ANTOINE_BUILD_LOG." >&2
      tail -40 "$ANTOINE_BUILD_LOG" >&2
      exit 1
    fi
    export ANTOINE_SITL_BUILD_RECEIPT="$ANTOINE_SOURCE_SNAPSHOT/antoine-source-build-receipt.json"
    export CARGO_NET_OFFLINE=true
    NATIVE_BUILD_RECEIPT_ARGS=(--native-build-receipt "$ANTOINE_SITL_BUILD_RECEIPT")
    if ! uv run --no-project python "$PROJECT_ROOT/research/verify_antoine_native_receipt.py" \
        --root "$RUNTIME_ANTOINE_ROOT" \
        --snapshot-root "$(dirname "$RUNTIME_ANTOINE_ROOT")" \
        --receipt "$ANTOINE_SITL_BUILD_RECEIPT" \
        >>"$ANTOINE_BUILD_LOG" 2>&1; then
      echo "Antoine native source-bound build failed; see $ANTOINE_BUILD_LOG." >&2
      tail -40 "$ANTOINE_BUILD_LOG" >&2
      exit 1
    fi
    mkdir -p "$RUN_CARGO_TARGET_DIR/debug"
    for binary in antoine_sitl_nats antoine_gz_local_authority antoine-boundary-service; do
      cp -p "$RUNTIME_ANTOINE_ROOT/.cargo-target/antoine-sitl-build-receipt/debug/$binary" \
        "$RUN_CARGO_TARGET_DIR/debug/$binary"
    done
  elif ! CARGO_TARGET_DIR="$ANTOINE_GZ_CARGO_TARGET_DIR" cargo build --locked --offline \
      --manifest-path "$RUNTIME_ANTOINE_ROOT/sitl/antoine-sitl/Cargo.toml" --features nats \
      --bin antoine_sitl_nats --bin antoine_gz_local_authority >"$ANTOINE_BUILD_LOG" 2>&1; then
    echo "Antoine locked preflight build failed; see $ANTOINE_BUILD_LOG." >&2
    tail -40 "$ANTOINE_BUILD_LOG" >&2
    exit 1
  fi
  if [[ ! -f "$RUNTIME_ANTOINE_ROOT/scripts/antoine_sitl_build_receipt.py" ]] && \
    ! CARGO_TARGET_DIR="$ANTOINE_GZ_CARGO_TARGET_DIR" cargo build --locked --offline \
      --manifest-path "$RUNTIME_ANTOINE_ROOT/runtime/antoine-boundary/Cargo.toml" --features service \
      --bin antoine-boundary-service >>"$ANTOINE_BUILD_LOG" 2>&1; then
    echo "Antoine boundary locked preflight build failed; see $ANTOINE_BUILD_LOG." >&2
    tail -40 "$ANTOINE_BUILD_LOG" >&2
    exit 1
  fi
  mkdir -p "$ANTOINE_GZ_PREBUILT_BIN_DIR"
  for binary in antoine_sitl_nats antoine_gz_local_authority antoine-boundary-service; do
    source_binary="$ANTOINE_GZ_CARGO_TARGET_DIR/debug/$binary"
    [[ -x "$source_binary" ]] || {
      echo "Run-local Antoine build did not produce executable $source_binary." >&2
      exit 1
    }
    cp -p "$source_binary" "$ANTOINE_GZ_PREBUILT_BIN_DIR/$binary"
  done
  SOURCE_REVISION="$(git -C "$BASE_ANTOINE_ROOT" rev-parse HEAD)"
  if ! uv run --project "$PROJECT_ROOT/research" python \
    "$PROJECT_ROOT/research/record_antoine_run_build.py" \
    --antoine-root "$RUNTIME_ANTOINE_ROOT" --binary-dir "$ANTOINE_GZ_PREBUILT_BIN_DIR" \
    --run-id "$RUN_ID_FOR_PROVENANCE" \
    --source-revision "$SOURCE_REVISION" \
    "${NATIVE_BUILD_RECEIPT_ARGS[@]}" \
    --output "$RUNTIME_SIMROOT/antoine-build-receipt.json" \
    >"$PROVENANCE_SNAPSHOT_DIR/build-receipt.json"; then
    echo "Could not bind run-owned Antoine binaries to their source and build receipt." >&2
    exit 1
  fi
  export ANTOINE_SOURCE_CONTEXT="$BASE_ANTOINE_ROOT"
  export ANTOINE_ROOT="$RUNTIME_ANTOINE_ROOT"
  write_public_profile_env >"$PROVENANCE_SNAPSHOT_DIR/research-profile.env"
  if ! uv run --project "$PROJECT_ROOT/research" python \
    "$PROJECT_ROOT/research/capture_antoine_provenance.py" \
    "$PROVENANCE_SNAPSHOT_DIR" --phase pre --run-id "$RUN_ID_FOR_PROVENANCE" \
    ${ANTOINE_PROVENANCE_EXTRA_ARGS:-} \
    --runtime-world "$RUNTIME_SIMROOT/runtime-worlds/antoine-gz-flight-proof.sdf" \
    --source-sim "$BASE_SIM_SOURCE" \
    "${PROVENANCE_GAZEBO_INPUT_ARGS[@]}" \
    "${PROVENANCE_SENSOR_INPUT_ARGS[@]}" \
    --expected-env "$PROVENANCE_SNAPSHOT_DIR/research-profile.env" \
    "${PROVENANCE_BUILD_INPUT_ARGS[@]}" \
    --output "$PROVENANCE_SNAPSHOT_DIR/research-provenance-pre.json" >/dev/null; then
    echo "Pre-run provenance capture failed; this run will remain unqualified." >&2
  fi
fi

PROOF_IDENTITY_PATH="$RUNTIME_STATE_DIR/proof-$$.identity"
(
  cd "$RUNTIME_ANTOINE_ROOT"
  dvd_run_owned_child "$PROJECT_ROOT" "$PROOF_IDENTITY_PATH" "$PROOF_SCRIPT" "${PROOF_ARGS[@]}"
) &
PROOF_PID=$!
if [[ -n "$REQUESTED_RUN_DIR" ]]; then
  if [[ "$REQUESTED_RUN_DIR" != /* ]]; then
    REQUESTED_RUN_DIR="$PWD/$REQUESTED_RUN_DIR"
  fi
  for _ in {1..100}; do
    [[ -d "$REQUESTED_RUN_DIR" ]] && break
    kill -0 "$PROOF_PID" 2>/dev/null || break
    sleep 0.1
  done
  if [[ -d "$REQUESTED_RUN_DIR" ]]; then
    cp "$PROVENANCE_SNAPSHOT_DIR/research-profile.env" \
      "$REQUESTED_RUN_DIR/research-profile.env" 2>/dev/null || true
    mkdir -p "$REQUESTED_RUN_DIR/gazebo-source-snapshot/worlds"
    cp -R "$RUNTIME_SIMROOT/models" "$REQUESTED_RUN_DIR/gazebo-source-snapshot/"
    cp -R "$RUNTIME_SIMROOT/tools" "$REQUESTED_RUN_DIR/gazebo-source-snapshot/"
    if [[ -d "$RUNTIME_SIMROOT/applier-build-source" ]]; then
      cp -R "$RUNTIME_SIMROOT/applier-build-source" "$REQUESTED_RUN_DIR/gazebo-source-snapshot/"
    fi
    cp "$RUNTIME_SIMROOT/worlds/antoine-gz-flight-proof.sdf" \
      "$REQUESTED_RUN_DIR/gazebo-source-snapshot/worlds/antoine-gz-flight-proof.sdf"
    cp "$RUNTIME_SIMROOT/gazebo-source-snapshot.json" \
      "$REQUESTED_RUN_DIR/gazebo-source-snapshot/gazebo-source-snapshot.json"
    cp "$RUNTIME_SIMROOT/gazebo-source-snapshot.json" \
      "$REQUESTED_RUN_DIR/research-gazebo-source-snapshot.json"
    cp -R "$ANTOINE_GZ_PREBUILT_BIN_DIR" "$REQUESTED_RUN_DIR/antoine-binaries"
    uv run --no-project python "$PROJECT_ROOT/research/seal_antoine_source_snapshot.py" \
      archive "$ANTOINE_SOURCE_SNAPSHOT" "$REQUESTED_RUN_DIR/antoine-source-snapshot"
    cp "$RUNTIME_SIMROOT/antoine-build-receipt.json" \
      "$REQUESTED_RUN_DIR/research-antoine-build-receipt.json"
    cp "$RUNTIME_SIMROOT/runtime-worlds/antoine-gz-flight-proof.sdf" \
      "$REQUESTED_RUN_DIR/research-runtime-world.sdf"
    if [[ -f "$PROVENANCE_SNAPSHOT_DIR/research-provenance-pre.json" ]]; then
      cp "$PROVENANCE_SNAPSHOT_DIR/research-provenance-pre.json" \
        "$REQUESTED_RUN_DIR/research-provenance-pre.json"
      if ! uv run --project "$PROJECT_ROOT/research" python \
          "$PROJECT_ROOT/research/capture_antoine_provenance.py" \
          "$REQUESTED_RUN_DIR" --phase pre --run-id "${REQUESTED_RUN_DIR##*/}" \
          ${ANTOINE_PROVENANCE_EXTRA_ARGS:-} \
          --runtime-world "$RUNTIME_SIMROOT/runtime-worlds/antoine-gz-flight-proof.sdf" \
          --source-sim "$BASE_SIM_SOURCE" \
          "${PROVENANCE_GAZEBO_INPUT_ARGS[@]}" \
          "${PROVENANCE_SENSOR_INPUT_ARGS[@]}" \
          --expected-env "$PROVENANCE_SNAPSHOT_DIR/research-profile.env" \
          "${PROVENANCE_BUILD_INPUT_ARGS[@]}" \
          --output "$REQUESTED_RUN_DIR/research-provenance-pre.json" >/dev/null; then
        echo "Run-local preflight provenance capture failed." >&2
      fi
    fi
    if [[ -n "$RUNTIME_PROFILE_DIR" && -f "$RUNTIME_PROFILE_DIR/gz_flight_proof.sh" ]]; then
      cp "$RUNTIME_PROFILE_DIR/gz_flight_proof.sh" \
        "$REQUESTED_RUN_DIR/research-sensor-profile-launcher.sh"
    fi
  else
    echo "Run-local world overlay was not copied into the requested evidence directory." >&2
  fi
fi
set +e
wait "$PROOF_PID"
PROOF_STATUS=$?
set -e
PROOF_PID=""
rm -f "$PROOF_IDENTITY_PATH"
if [[ -n "$REQUESTED_RUN_DIR" && -d "$REQUESTED_RUN_DIR" ]]; then
  # Sample postflight state only after Antoine and the flight proof have exited.
  # The overlay and profile snapshot remain available until cleanup below.
  export ANTOINE_GZ_SENSOR_PROFILE_LAUNCHER="${ANTOINE_GZ_SENSOR_PROFILE_LAUNCHER:-}"
  export FURIA_GZ_SENSOR_FORWARDER_BIN="${FURIA_GZ_SENSOR_FORWARDER_BIN:-$BASE_SIM_SOURCE/target/gz-nats-bridge/debug/gz-nav-sensor-forwarder}"
  export AGR_SIM="$BASE_SIM_SOURCE"
  export AGR_C2_SCRIPTS="${AGR_C2_SCRIPTS:-$HOME/Work/furia-c2/scripts}"
  export GZ_BIN="${GZ_BIN:-$HOME/Work/gz-macOS/ws/install/bin/gz}"
  export ANTOINE_GZ_PHYSICS_ENGINE="${ANTOINE_GZ_PHYSICS_ENGINE:-gz-physics-bullet-featherstone-plugin}"
  if [[ -f "$REQUESTED_RUN_DIR/research-provenance-pre.json" ]]; then
    if ! uv run --project "$PROJECT_ROOT/research" python \
      "$PROJECT_ROOT/research/capture_antoine_provenance.py" "$REQUESTED_RUN_DIR" \
      --phase post --run-id "${REQUESTED_RUN_DIR##*/}" \
      ${ANTOINE_PROVENANCE_EXTRA_ARGS:-} \
      --runtime-world "$RUNTIME_SIMROOT/runtime-worlds/antoine-gz-flight-proof.sdf" \
      --source-sim "$BASE_SIM_SOURCE" \
      "${PROVENANCE_GAZEBO_INPUT_ARGS[@]}" \
      "${PROVENANCE_SENSOR_INPUT_ARGS[@]}" \
      --expected-env "$PROVENANCE_SNAPSHOT_DIR/research-profile.env" \
      "${PROVENANCE_BUILD_INPUT_ARGS[@]}" \
      --output "$REQUESTED_RUN_DIR/research-provenance.json" >/dev/null; then
      echo "Postflight provenance capture failed; this run will remain unqualified." >&2
    fi
  fi
fi
PROOF_PID=""
exit "$PROOF_STATUS"
