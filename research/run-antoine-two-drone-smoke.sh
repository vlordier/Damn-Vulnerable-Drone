#!/usr/bin/env bash
# Bounded smoke run for two separate X500 models and NATS state subjects.
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$PROJECT_ROOT/research/owned_process.sh"
SIM_SOURCE="${AGR_SIM:-$HOME/Work/durandal-gz-swarm-sim}"
C2_SCRIPTS="${AGR_C2_SCRIPTS:-$HOME/Work/furia-c2/scripts}"
GZ_BIN="${GZ_BIN:-$HOME/Work/gz-macOS/ws/install/bin/gz}"
GZ_INSTALL="$(cd "$(dirname "$GZ_BIN")/.." && pwd)"
REPORT_ROOT="${ANTOINE_REPORT_DIR:-$PROJECT_ROOT/build_logs/research/antoine-scenarios}"
RUN_ID="antoine-two-drone-smoke-$(date -u +%Y%m%dT%H%M%SZ)-$$"
RUN_DIR="$REPORT_ROOT/$RUN_ID"
SERVER_PORT="${ANTOINE_TWO_DRONE_NATS_PORT:-$(uv run --no-project python -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')}"
[[ "$SERVER_PORT" =~ ^[0-9]+$ ]] && ((SERVER_PORT >= 1 && SERVER_PORT <= 65535)) || {
  echo "Invalid run-local NATS port: $SERVER_PORT" >&2
  exit 2
}
SERVER="nats://127.0.0.1:$SERVER_PORT"
PARTITION="antoine-two-drone-$$"
NATS_PID=""
SIM_PID=""
BRIDGE_PID=""
APPLIER_PID=""
NATS_ID="$REPORT_ROOT/.$RUN_ID-nats.identity"
SIM_ID="$REPORT_ROOT/.$RUN_ID-sim.identity"
BRIDGE_ID="$REPORT_ROOT/.$RUN_ID-bridge.identity"
APPLIER_ID="$REPORT_ROOT/.$RUN_ID-applier.identity"

for tool in nats nats-server uv; do
  command -v "$tool" >/dev/null 2>&1 || { echo "Required command not found: $tool" >&2; exit 1; }
done
[[ -x "$GZ_BIN" ]] || { echo "Gazebo executable not found: $GZ_BIN" >&2; exit 1; }
for input in "$SIM_SOURCE/worlds/antoine-gz-flight-proof.sdf" "$SIM_SOURCE/models/x500/model.sdf" "$C2_SCRIPTS/c2-gz-sitl-bridge.py"; do
  [[ -f "$input" ]] || { echo "Missing required input: $input" >&2; exit 1; }
done
APPLIER_BIN="$SIM_SOURCE/tools/gz-sitl-applier/gz-sitl-applier"
[[ -x "$APPLIER_BIN" ]] || { echo "Gazebo actuator applier not found: $APPLIER_BIN" >&2; exit 1; }
export AGR_SIM="$SIM_SOURCE"
export AGR_C2_SCRIPTS="$C2_SCRIPTS"
export GZ_BIN
export ANTOINE_GZ_FLIGHT_NATS_PORT="$SERVER_PORT"
mkdir -p "$REPORT_ROOT"
mkdir "$RUN_DIR"
uv run --no-project python "$PROJECT_ROOT/research/prepare_antoine_two_drone_world.py" \
  "$SIM_SOURCE/worlds/antoine-gz-flight-proof.sdf" "$RUN_DIR/two-drone-world.sdf" \
  >"$RUN_DIR/world-build.log"

set +u
source "$GZ_INSTALL/setup.bash"
set -u
export GZ_IP=127.0.0.1
export GZ_PARTITION="$PARTITION"
export GZ_CLI="$GZ_BIN"
export GZ_INSTALL
export GZ_SIM_RESOURCE_PATH="$SIM_SOURCE/models${GZ_SIM_RESOURCE_PATH:+:$GZ_SIM_RESOURCE_PATH}"
export DYLD_LIBRARY_PATH="$GZ_INSTALL/lib${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"
{
  printf 'GZ_IP=%s\n' "$GZ_IP"
  printf 'GZ_PARTITION=%s\n' "$GZ_PARTITION"
  printf 'GZ_CLI=%s\n' "$GZ_CLI"
  printf 'GZ_INSTALL=%s\n' "$GZ_INSTALL"
  printf 'GZ_SIM_RESOURCE_PATH=%s\n' "$GZ_SIM_RESOURCE_PATH"
  printf 'ANTOINE_GZ_FLIGHT_NATS_PORT=%s\n' "$ANTOINE_GZ_FLIGHT_NATS_PORT"
} >"$RUN_DIR/research-profile.env"
PROVENANCE_ARGS=(
  "$RUN_DIR" --run-id "$RUN_ID" --source-sim "$SIM_SOURCE"
  --runtime-world "$RUN_DIR/two-drone-world.sdf"
  --input-world "$SIM_SOURCE/worlds/antoine-gz-flight-proof.sdf"
  --input-models-dir "$SIM_SOURCE/models" --input-applier "$APPLIER_BIN"
  --exclude-antoine --exclude-qgroundcontrol --expected-env "$RUN_DIR/research-profile.env"
  --require-input gazebo_binary --require-input source_world
  --require-input runtime_world_overlay --require-input x500_model
  --require-input gazebo_model_assets --require-input gazebo_applier
  --require-input c2_gazebo_bridge --require-input c2_python_requirements
  --require-input nats_executable --require-input nats-server_executable --require-input uv_executable
)
uv run --no-project python "$PROJECT_ROOT/research/capture_antoine_provenance.py" \
  "${PROVENANCE_ARGS[@]}" --phase pre --output "$RUN_DIR/research-provenance-pre.json" >/dev/null

cleanup() {
  local status=$?
  [[ -z "$BRIDGE_PID" ]] || dvd_signal_owned_child "$PROJECT_ROOT" "$BRIDGE_PID" "$BRIDGE_ID" TERM || true
  [[ -z "$APPLIER_PID" ]] || dvd_signal_owned_child "$PROJECT_ROOT" "$APPLIER_PID" "$APPLIER_ID" TERM || true
  [[ -z "$SIM_PID" ]] || dvd_signal_owned_child "$PROJECT_ROOT" "$SIM_PID" "$SIM_ID" TERM || true
  [[ -z "$NATS_PID" ]] || dvd_signal_owned_child "$PROJECT_ROOT" "$NATS_PID" "$NATS_ID" TERM || true
  [[ -z "$BRIDGE_PID" ]] || wait "$BRIDGE_PID" 2>/dev/null || true
  [[ -z "$APPLIER_PID" ]] || wait "$APPLIER_PID" 2>/dev/null || true
  [[ -z "$SIM_PID" ]] || wait "$SIM_PID" 2>/dev/null || true
  [[ -z "$NATS_PID" ]] || wait "$NATS_PID" 2>/dev/null || true
  rm -f "$APPLIER_ID" "$BRIDGE_ID" "$SIM_ID" "$NATS_ID"
  return "$status"
}
trap cleanup EXIT INT TERM

dvd_run_owned_child "$PROJECT_ROOT" "$NATS_ID" nats-server -a 127.0.0.1 -p "$SERVER_PORT" \
  -js -sd "$RUN_DIR/jetstream" >"$RUN_DIR/nats.log" 2>&1 &
NATS_PID=$!
ready=0
for _ in {1..80}; do
  if grep -Fq "Listening for client connections on 127.0.0.1:$SERVER_PORT" "$RUN_DIR/nats.log" \
    && grep -Fq 'Server is ready' "$RUN_DIR/nats.log" \
    && nats --no-context --server "$SERVER" rtt >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 0.25
done
[[ "$ready" == 1 ]] || { echo "run-owned NATS broker did not become ready" >&2; exit 1; }

dvd_run_owned_child "$PROJECT_ROOT" "$SIM_ID" "$GZ_BIN" sim -s -r \
  --physics-engine gz-physics-bullet-featherstone-plugin "$RUN_DIR/two-drone-world.sdf" \
  >"$RUN_DIR/gazebo.log" 2>&1 &
SIM_PID=$!
ready=0
for _ in {1..80}; do
  if timeout 4 "$GZ_BIN" topic --list 2>/dev/null | grep -q '^/world/antoine_gz_flight_proof/pose/info$'; then ready=1; break; fi
  sleep 0.25
done
[[ "$ready" == 1 ]] || { echo "two-drone Gazebo world did not become ready" >&2; exit 1; }

# Verify the actuator layer can address both model-specific Gazebo command
# topics. No Antoine controller or non-neutral actuator command is started.
mkdir -p "$RUN_DIR/applier-neutral" "$RUN_DIR/applier-authorized"
export NATS_URL="$SERVER"
export DVD_APPLIER_BIN="$APPLIER_BIN"
export RUN_ID
GZ_SITL_WORLD_SHA256="sha256:$(shasum -a 256 "$RUN_DIR/two-drone-world.sdf" | cut -d' ' -f1)"
export GZ_SITL_WORLD_SHA256
export FURIA_PRIME_ENU_FRAME_ID="enu-48.0002-0.3564-82"
export FURIA_GZ_APPLIER_NEUTRAL_DIR="$RUN_DIR/applier-neutral"
export FURIA_GZ_APPLIER_AUTHORIZED_DIR="$RUN_DIR/applier-authorized"
export FURIA_SITL_RUN_ID="$RUN_ID"
FURIA_ANTOINE_GZ_COMMAND_PUBLIC_KEY_HEX="$(uv run --project "$PROJECT_ROOT/research" python -c 'import secrets; from nacl.signing import SigningKey; print(SigningKey(secrets.token_bytes(32)).verify_key.encode().hex())')"
export FURIA_ANTOINE_GZ_COMMAND_PUBLIC_KEY_HEX
FURIA_CORE_EXECUTION_GRANT_PUBLIC_KEY_HEX="$(uv run --project "$PROJECT_ROOT/research" python -c 'import secrets; from nacl.signing import SigningKey; print(SigningKey(secrets.token_bytes(32)).verify_key.encode().hex())')"
export FURIA_CORE_EXECUTION_GRANT_PUBLIC_KEY_HEX
# Variables in this command intentionally expand inside the child shell.
# shellcheck disable=SC2016
dvd_run_owned_child "$PROJECT_ROOT" "$APPLIER_ID" bash -c 'nats --no-context --server "$NATS_URL" sub --raw "furia.gzsitl.commands.>" 2>/dev/null | env \
  DYLD_LIBRARY_PATH="$GZ_INSTALL/lib:${DYLD_LIBRARY_PATH:-}" \
  FURIA_ANTOINE_GZ_COMMAND_PUBLIC_KEY_HEX="$FURIA_ANTOINE_GZ_COMMAND_PUBLIC_KEY_HEX" \
  FURIA_CORE_EXECUTION_GRANT_PUBLIC_KEY_HEX="$FURIA_CORE_EXECUTION_GRANT_PUBLIC_KEY_HEX" \
  FURIA_GZ_APPLIER_NEUTRAL_DIR="$FURIA_GZ_APPLIER_NEUTRAL_DIR" \
  FURIA_GZ_APPLIER_AUTHORIZED_DIR="$FURIA_GZ_APPLIER_AUTHORIZED_DIR" \
  FURIA_SITL_RUN_ID="$FURIA_SITL_RUN_ID" GZ_SITL_WORLD_SHA256="$GZ_SITL_WORLD_SHA256" \
  FURIA_PRIME_ENU_FRAME_ID="$FURIA_PRIME_ENU_FRAME_ID" "$DVD_APPLIER_BIN" --agents isr,fpv-a --verbose' \
  >"$RUN_DIR/applier.log" 2>&1 &
APPLIER_PID=$!
ready=0
for _ in {1..160}; do
  if grep -q 'startup-neutral-zero-published drone-1' "$RUN_DIR/applier.log" \
    && grep -q 'startup-neutral-zero-published drone-2' "$RUN_DIR/applier.log"; then ready=1; break; fi
  if ! kill -0 "$APPLIER_PID" 2>/dev/null; then break; fi
  sleep 0.25
done
[[ "$ready" == 1 ]] || { echo "actuator applier did not attest both vehicles; see $RUN_DIR/applier.log" >&2; tail -30 "$RUN_DIR/applier.log" >&2; exit 1; }

dvd_run_owned_child "$PROJECT_ROOT" "$BRIDGE_ID" uv run --no-project \
  --with-requirements "$C2_SCRIPTS/../requirements-gz-sitl.txt" \
  "$C2_SCRIPTS/c2-gz-sitl-bridge.py" --nats-url "$SERVER" \
  --world antoine_gz_flight_proof --agents isr,fpv-a --home 48.0002,0.3564,82.0 \
  --frame-id enu-48.0002-0.3564-82 --state-hz 20 --no-telemetry \
  >"$RUN_DIR/bridge.log" 2>&1 &
BRIDGE_PID=$!
set +e
uv run --no-project python "$PROJECT_ROOT/research/antoine_two_drone_probe.py" \
  --gz "$GZ_BIN" --server "$SERVER" --run-dir "$RUN_DIR" --applier-log "$RUN_DIR/applier.log"
PROBE_STATUS=$?
set -e
cleanup
trap - EXIT INT TERM
set +e
uv run --no-project python "$PROJECT_ROOT/research/capture_antoine_provenance.py" \
  "${PROVENANCE_ARGS[@]}" --phase post --output "$RUN_DIR/research-provenance.json" >/dev/null
PROVENANCE_CAPTURE_STATUS=$?
set -e
set +e
uv run --project "$PROJECT_ROOT/research" python \
  "$PROJECT_ROOT/research/finalize_antoine_two_drone_smoke.py" \
  --run-dir "$RUN_DIR" --capture-exit-code "$PROVENANCE_CAPTURE_STATUS"
FINALIZE_STATUS=$?
set -e
uv run --no-project python "$PROJECT_ROOT/research/build_evidence_manifest.py" "$RUN_DIR" >/dev/null
printf 'two-drone smoke run: %s\n' "$RUN_DIR"
cat "$RUN_DIR/research-two-drone-smoke.json"
if [[ "$PROBE_STATUS" -ne 0 ]]; then exit "$PROBE_STATUS"; fi
exit "$FINALIZE_STATUS"
