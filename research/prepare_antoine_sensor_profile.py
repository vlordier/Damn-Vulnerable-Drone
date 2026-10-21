#!/usr/bin/env python3
"""Create a run-local proof launcher overlay that owns the signed sensor forwarder."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

ROOT_ASSIGNMENT = 'ANTOINE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"'
ANCHOR = "# What the engine was, recorded next to the evidence: the report contract has no\n"
GAZEBO_LAUNCH = '"$GZ_BIN" sim -s -r "${SIM_ARGS[@]+"${SIM_ARGS[@]}"}" "$WORLD_FILE" >"$LOG_DIR/gazebo.log" 2>&1 &'
GZ_RUNTIME_ENV = 'export DYLD_LIBRARY_PATH="$GZ_INSTALL/lib:${DYLD_LIBRARY_PATH:-}"'
RENDERER_ENV_BLOCK = """# Checkout-local rendering runtime, applied after setup.bash resets Gazebo paths.
if [ "${ANTOINE_GZ_CAMERA_RENDERING:-0}" = "1" ]; then
  RENDERING_PREFIX="${FURIA_GZ_RENDERING_PREFIX:?FURIA_GZ_RENDERING_PREFIX must point to the checkout-local Ogre2 runtime}"
  RENDER_ENGINE_DIR="${FURIA_GZ_RENDER_ENGINE_PATH:-$RENDERING_PREFIX/lib/gz-rendering-8/engine-plugins}"
  RENDERING_CORE="$RENDERING_PREFIX/lib/libgz-rendering8.8.dylib"
  RENDERING_SENSOR_OVERLAY="$RENDERING_PREFIX/lib/libgz-sim8-sensors-system.dylib"
  if [ ! -f "$RENDERING_CORE" ] || [ ! -f "$RENDERING_SENSOR_OVERLAY" ] || [ ! -d "$RENDER_ENGINE_DIR" ]; then
    echo "The checkout-local Ogre2 rendering runtime is incomplete under $RENDERING_PREFIX." >&2
    exit 1
  fi
  export FURIA_GZ_RENDERING_PREFIX="$RENDERING_PREFIX"
  export FURIA_GZ_RENDER_ENGINE_PATH="$RENDER_ENGINE_DIR"
  export GZ_SIM_RENDER_ENGINE_PATH="$RENDER_ENGINE_DIR"
  export GZ_RENDERING_PLUGIN_PATH="$RENDER_ENGINE_DIR"
  export GZ_SIM_SYSTEM_PLUGIN_PATH="$RENDERING_PREFIX/lib:${GZ_SIM_SYSTEM_PLUGIN_PATH:-}"
  export DYLD_LIBRARY_PATH="$RENDERING_PREFIX/lib:$GZ_INSTALL/lib:${DYLD_LIBRARY_PATH:-}"
  export DYLD_INSERT_LIBRARIES="$RENDERING_CORE${DYLD_INSERT_LIBRARIES:+:$DYLD_INSERT_LIBRARIES}"
fi
"""
FORWARDER_BLOCK = """# Checkout-local research hook: own the pinned sensor forwarder in this proof run.
if [ "${ANTOINE_GZ_SENSOR_FORWARDING:-0}" = "1" ]; then
  step "starting source-bound signed Gazebo sensor forwarder"
  cp "$ANTOINE_GZ_SENSOR_PROFILE_JSON" "$RUN_DIR/sensor-profile.json"
  printf 'GZ_PARTITION=%s\\n' "$GZ_PARTITION" >>"$RUN_DIR/sensor-profile.json"
  start_owned "$FURIA_GZ_SENSOR_FORWARDER_BIN" --world "$WORLD_NAME" --nats-url "$NATS_URL" \\
    --vehicle "$CALLSIGN=$AGENT" >"$LOG_DIR/gz-nav-sensor-forwarder.log" 2>&1
  SENSOR_FORWARDER_IDENTITY="$LAST_OWNED_IDENTITY"
  for _ in {1..20}; do
    [ -f "$SENSOR_FORWARDER_IDENTITY" ] && break
    kill -0 "$LAST_OWNED_PID" 2>/dev/null || break
    sleep 0.05
  done
  SENSOR_FORWARDER_PID="$(sed -n 's/^child_pid=//p' "$SENSOR_FORWARDER_IDENTITY" 2>/dev/null)"
  [ -n "$SENSOR_FORWARDER_PID" ] || { echo "sensor forwarder supervisor did not publish its child identity" >&2; exit 1; }
  printf '%s\\n' "$SENSOR_FORWARDER_PID" >"$RUN_DIR/sensor-forwarder.pid"
  printf '%s\\n' "$SENSOR_FORWARDER_IDENTITY" >"$RUN_DIR/sensor-forwarder.identity-path"
  wait_for "signed Gazebo sensor streams" 30 \\
    "grep -q 'Gazebo IMU/NavSat/air-pressure streams ready; sensor-only forwarding enabled' '$LOG_DIR/gz-nav-sensor-forwarder.log' 2>/dev/null"
fi

"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("antoine_root", type=Path)
    args = parser.parse_args()

    source = args.source.resolve()
    destination = args.destination.resolve()
    antoine_root = args.antoine_root.resolve()
    text = source.read_text(encoding="utf-8")
    if text.count(ROOT_ASSIGNMENT) != 1:
        raise SystemExit("upstream proof root assignment changed; refusing an unreviewed patch")
    if text.count(ANCHOR) != 1:
        raise SystemExit(
            "upstream proof engine-evidence anchor changed; refusing an unreviewed patch"
        )
    if text.count(GAZEBO_LAUNCH) != 1:
        raise SystemExit(
            "upstream Gazebo launch command changed; refusing an unreviewed rendering patch"
        )
    if text.count(GZ_RUNTIME_ENV) != 1:
        raise SystemExit(
            "upstream Gazebo runtime environment changed; refusing an unreviewed renderer-path patch"
        )
    text = text.replace(ROOT_ASSIGNMENT, f'ANTOINE_ROOT="{antoine_root}"', 1)
    text = text.replace(GZ_RUNTIME_ENV, GZ_RUNTIME_ENV + "\n" + RENDERER_ENV_BLOCK, 1)
    if os.environ.get("ANTOINE_GZ_CAMERA_RENDERING") == "1":
        rendered_launch = GAZEBO_LAUNCH.replace(
            "sim -s -r ",
            "sim -s -r --headless-rendering --render-engine-server=ogre2 "
            "--render-engine-server-api-backend=metal ",
        )
        text = text.replace(GAZEBO_LAUNCH, rendered_launch, 1)
    text = text.replace(ANCHOR, FORWARDER_BLOCK + ANCHOR, 1)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8")
    destination.chmod(0o700)
    details = {
        "contract": "damn-vulnerable-drone.antoine-sensor-profile-overlay",
        "version": "1.0.0",
        "sourcePath": str(source),
        "sourceSha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "overlaySha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
        "controllerHz": os.environ.get("ANTOINE_GZ_FLIGHT_CONTROL_HZ", "25"),
        "patch": "preserve source root, optionally select the checkout-local Ogre2 runtime after Gazebo setup resets loader paths, optionally enable headless Ogre2 server rendering with the Metal backend, record the run partition, and start the run-owned signed Gazebo sensor forwarder after physics engine validation",
        "sourceModified": False,
    }
    (destination.parent / "profile.json").write_text(
        json.dumps(details, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
