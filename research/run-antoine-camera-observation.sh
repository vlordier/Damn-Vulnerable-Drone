#!/usr/bin/env bash
# Capture bounded X500 camera metadata on a run-owned, camera-only Gazebo partition.
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$PROJECT_ROOT/research/owned_process.sh"
SIM_SOURCE="${AGR_SIM:-$HOME/Work/durandal-gz-swarm-sim}"
GZ_BIN="${GZ_BIN:-$HOME/Work/gz-macOS/ws/install/bin/gz}"
GZ_INSTALL="$(cd "$(dirname "$GZ_BIN")/.." && pwd)"
REPORT_ROOT="${ANTOINE_REPORT_DIR:-$PROJECT_ROOT/build_logs/research/antoine-scenarios}"
RENDERING_PREFIX="${FURIA_GZ_RENDERING_PREFIX:-$PROJECT_ROOT/runs/gz-rendering-ogre2-prefix}"
RENDERING_BUILD_DIR="${FURIA_GZ_RENDERING_BUILD_DIR:-$PROJECT_ROOT/runs/gz-rendering-ogre2-build}"
RUN_ID="antoine-camera-observation-$(date -u +%Y%m%dT%H%M%SZ)-$$"
RUN_DIR="$REPORT_ROOT/$RUN_ID"
CAMERA_TOPIC="/world/antoine_gz_flight_proof/model/isr/link/base_link/sensor/camera/image"
CAMERA_PARTITION="antoine-camera-$$"
SIM_PID=""
PROBE_PID=""
SIM_IDENTITY="$REPORT_ROOT/.$RUN_ID-sim.identity"
PROBE_IDENTITY="$REPORT_ROOT/.$RUN_ID-probe.identity"

[[ ! -e "$RUN_DIR" ]] || { echo "Refusing to overwrite research evidence: $RUN_DIR" >&2; exit 1; }
for tool in uv bash; do
  command -v "$tool" >/dev/null 2>&1 || { printf 'Required command not found: %s\n' "$tool" >&2; exit 1; }
done
[[ -x "$GZ_BIN" ]] || { printf 'Gazebo executable not found: %s\n' "$GZ_BIN" >&2; exit 1; }
mkdir -p "$RUN_DIR"

RENDERER_BUILD_SCRIPT="$SIM_SOURCE/tools/build-compatible-ogre2-renderer.sh"
[[ -f "$RENDERER_BUILD_SCRIPT" ]] || { printf 'Missing renderer builder: %s\n' "$RENDERER_BUILD_SCRIPT" >&2; exit 1; }
export FURIA_RENDERING_PREFIX="$RENDERING_PREFIX"
export FURIA_RENDERING_BUILD_DIR="$RENDERING_BUILD_DIR"
export FURIA_GZ_RENDERING_PREFIX="$RENDERING_PREFIX"
export FURIA_GZ_RENDERING_BUILD_DIR="$RENDERING_BUILD_DIR"
bash "$RENDERER_BUILD_SCRIPT" >"$RUN_DIR/renderer-build.log" 2>&1
env | LC_ALL=C sort | sed -n '/^FURIA_GZ_RENDERING_PREFIX=/p' >"$RUN_DIR/research-profile.env"

uv run --no-project python "$PROJECT_ROOT/research/prepare_antoine_camera_world.py" \
  "$SIM_SOURCE/worlds/antoine-gz-flight-proof.sdf" \
  "$SIM_SOURCE/models/x500/model.sdf" "$RUN_DIR/camera-world.sdf" \
  >"$RUN_DIR/camera-world-build.log"

# Use the source Gazebo environment, then restore the renderer paths after its
# setup script so the server binds to this checkout's compatible Ogre2 overlay.
set +u
source "$GZ_INSTALL/setup.bash"
set -u
RENDER_ENGINE_DIR="$RENDERING_PREFIX/lib/gz-rendering-8/engine-plugins"
RENDERING_CORE="$RENDERING_PREFIX/lib/libgz-rendering8.8.dylib"
[[ -f "$RENDERING_CORE" && -f "$RENDERING_PREFIX/lib/libgz-sim8-sensors-system.dylib" && -d "$RENDER_ENGINE_DIR" ]] || {
  printf 'Incomplete local Ogre2 runtime under %s\n' "$RENDERING_PREFIX" >&2
  exit 1
}
export GZ_IP=127.0.0.1
export GZ_PARTITION="$CAMERA_PARTITION"
export GZ_SIM_RESOURCE_PATH="$SIM_SOURCE/models${GZ_SIM_RESOURCE_PATH:+:$GZ_SIM_RESOURCE_PATH}"
export GZ_SIM_SYSTEM_PLUGIN_PATH="$RENDERING_PREFIX/lib:$GZ_INSTALL/lib${GZ_SIM_SYSTEM_PLUGIN_PATH:+:$GZ_SIM_SYSTEM_PLUGIN_PATH}"
export GZ_SIM_RENDER_ENGINE_PATH="$RENDER_ENGINE_DIR"
export GZ_RENDERING_PLUGIN_PATH="$RENDER_ENGINE_DIR"
export DYLD_LIBRARY_PATH="$RENDERING_PREFIX/lib:$GZ_INSTALL/lib${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"
export DYLD_INSERT_LIBRARIES="$RENDERING_CORE${DYLD_INSERT_LIBRARIES:+:$DYLD_INSERT_LIBRARIES}"
env | LC_ALL=C sort | sed -n '/^FURIA_GZ_RENDERING/p; /^GZ_/p' >"$RUN_DIR/research-profile.env"

cleanup() {
  local status=$?
  [[ -z "$PROBE_PID" ]] || dvd_signal_owned_child "$PROJECT_ROOT" "$PROBE_PID" "$PROBE_IDENTITY" TERM || true
  [[ -z "$SIM_PID" ]] || dvd_signal_owned_child "$PROJECT_ROOT" "$SIM_PID" "$SIM_IDENTITY" TERM || true
  [[ -z "$PROBE_PID" ]] || wait "$PROBE_PID" 2>/dev/null || true
  [[ -z "$SIM_PID" ]] || wait "$SIM_PID" 2>/dev/null || true
  rm -f "$PROBE_IDENTITY" "$SIM_IDENTITY"
  return "$status"
}
trap cleanup EXIT INT TERM

dvd_run_owned_child "$PROJECT_ROOT" "$SIM_IDENTITY" "$GZ_BIN" sim -s -r --headless-rendering --render-engine-server=ogre2 \
  --render-engine-server-api-backend=metal \
  "$RUN_DIR/camera-world.sdf" >"$RUN_DIR/gazebo-camera.log" 2>&1 &
SIM_PID=$!
dvd_run_owned_child "$PROJECT_ROOT" "$PROBE_IDENTITY" uv run --no-project python "$PROJECT_ROOT/research/antoine_camera_probe.py" \
  --gz "$GZ_BIN" --topic "$CAMERA_TOPIC" --run-dir "$RUN_DIR" \
  --startup-timeout 15 --timeout 10 >"$RUN_DIR/camera-probe.log" 2>&1 &
PROBE_PID=$!
set +e
wait "$PROBE_PID"
PROBE_STATUS=$?
PROBE_PID=""
rm -f "$PROBE_IDENTITY"
set -e
uv run --project "$PROJECT_ROOT/research" python "$PROJECT_ROOT/research/capture_antoine_provenance.py" \
  "$RUN_DIR" --camera-only --phase pre --run-id "$RUN_ID" \
  --runtime-world "$RUN_DIR/camera-world.sdf" \
  --input-world "$SIM_SOURCE/worlds/antoine-gz-flight-proof.sdf" \
  --expected-env "$RUN_DIR/research-profile.env" \
  --output "$RUN_DIR/research-provenance-pre.json" >/dev/null
uv run --project "$PROJECT_ROOT/research" python "$PROJECT_ROOT/research/capture_antoine_provenance.py" \
  "$RUN_DIR" --camera-only --phase post --run-id "$RUN_ID" \
  --runtime-world "$RUN_DIR/camera-world.sdf" \
  --input-world "$SIM_SOURCE/worlds/antoine-gz-flight-proof.sdf" \
  --expected-env "$RUN_DIR/research-profile.env" \
  --output "$RUN_DIR/research-provenance.json" >/dev/null
uv run --no-project python "$PROJECT_ROOT/research/build_evidence_manifest.py" "$RUN_DIR" >/dev/null
printf 'camera observation run: %s\n' "$RUN_DIR"
printf 'camera probe exit: %s (0=captured; 2=inconclusive bounded window)\n' "$PROBE_STATUS"
cat "$RUN_DIR/research-camera-observation.json" 2>/dev/null || cat "$RUN_DIR/camera-probe.log"
exit "$PROBE_STATUS"
